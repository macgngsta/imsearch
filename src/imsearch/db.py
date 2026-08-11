"""Read-only access to the macOS iMessage database (~/Library/Messages/chat.db).

The database is opened strictly read-only. On modern macOS the message text lives
in the ``attributedBody`` BLOB (a NeXTSTEP typedstream / NSAttributedString), not the
plain ``text`` column, so we decode it with ``typedstream``.
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import typedstream
from typedstream.types.foundation import NSString

# Messages stores `date` as nanoseconds since the Apple/Cocoa epoch (2001-01-01 UTC).
APPLE_EPOCH_OFFSET = 978307200  # seconds between Unix epoch and 2001-01-01 UTC

DEFAULT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"


ATTACHMENTS_ROOT = (Path.home() / "Library" / "Messages" / "Attachments").resolve()


@dataclass(frozen=True)
class Attachment:
    name: str | None  # human-readable name (transfer_name)
    mime_type: str | None  # e.g. image/jpeg; falls back to the UTI
    total_bytes: int | None
    path: str | None  # on-disk location (usually under ~/Library/Messages/Attachments)
    rowid: int | None = None  # attachment.ROWID, used to serve the file by id

    def resolved_path(self) -> Path | None:
        """Absolute on-disk path with ``~`` expanded, or None if there is no file."""
        return Path(self.path).expanduser() if self.path else None

    @property
    def is_image(self) -> bool:
        return bool(self.mime_type and self.mime_type.startswith("image/"))


def attachment_path(conn: sqlite3.Connection, attachment_id: int) -> Path | None:
    """Resolve an attachment ROWID to a safe on-disk path, or None.

    Guards against path traversal: the resolved file must live under the Messages
    Attachments directory and exist. Callers (e.g. the web server) can trust the result.
    """
    row = conn.execute(
        "SELECT filename FROM attachment WHERE ROWID = ?", (attachment_id,)
    ).fetchone()
    if not row or not row[0]:
        return None
    path = Path(row[0]).expanduser().resolve()
    if not path.is_relative_to(ATTACHMENTS_ROOT) or not path.is_file():
        return None
    return path


@dataclass(frozen=True)
class Message:
    rowid: int
    text: str
    date: datetime | None
    is_from_me: bool
    handle: str | None  # phone/email of the other party
    chat_name: str | None  # group display name or chat identifier
    chat_id: int | None = None  # chat.ROWID, used to scope conversation context
    has_attachment: bool = False
    date_raw: int | None = None  # exact ns-since-2001 anchor (avoids float round-trip)
    is_deleted: bool = False  # in the "Recently Deleted" store, not the live conversation


def apple_time_to_datetime(raw: int | None) -> datetime | None:
    """Convert a Messages `date` value (ns since 2001) to a local-time datetime."""
    if not raw:
        return None
    seconds = raw / 1_000_000_000 + APPLE_EPOCH_OFFSET
    return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone()


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Extract the message text from an ``attributedBody`` typedstream blob."""
    if not blob:
        return None
    try:
        obj = typedstream.unarchive_from_data(blob)
    except Exception:
        return None
    for item in getattr(obj, "contents", []):
        value = getattr(item, "value", None)
        if isinstance(value, NSString):
            return value.value
    return None


def message_text(text: str | None, attributed_body: bytes | None) -> str | None:
    """Prefer the plain ``text`` column, fall back to decoding ``attributedBody``."""
    if text:
        return text
    return decode_attributed_body(attributed_body)


def _has_pending_wal(db_path: Path) -> bool:
    """True if a non-empty write-ahead log sits beside the database."""
    wal = db_path.with_name(db_path.name + "-wal")
    try:
        return wal.exists() and wal.stat().st_size > 0
    except OSError:
        return False


def _require_exists(db_path: Path) -> None:
    if not db_path.exists():
        raise FileNotFoundError(
            f"iMessage database not found at {db_path}. "
            "This tool only works on macOS with Messages configured."
        )


def connect_readonly(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a database strictly read-only, ignoring any pending WAL (fast path).

    ``immutable=1`` tells SQLite the file will not change so it reads past the lock
    Messages.app holds without copying. It also ignores the ``-wal`` sidecar, so this
    misses messages not yet checkpointed — use :func:`open_readonly` when that matters.
    """
    _require_exists(db_path)
    return sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)


@contextlib.contextmanager
def open_readonly(db_path: Path = DEFAULT_DB_PATH):
    """Open a database read-only, including rows still in the write-ahead log.

    Messages.app keeps recent messages in a ``-wal`` sidecar until it checkpoints them
    into the main file. ``immutable=1`` would ignore that sidecar and hide the newest
    messages, so when a non-empty ``-wal`` exists we copy the database and its sidecars
    to a private temp file and read that (with ``query_only`` so we never write). The
    source database is only ever read from, never modified.
    """
    _require_exists(db_path)
    if not _has_pending_wal(db_path):
        conn = connect_readonly(db_path)
        try:
            yield conn
        finally:
            conn.close()
        return

    tmp = tempfile.mkdtemp(prefix="imsearch-")
    try:
        snapshot = Path(tmp) / db_path.name
        shutil.copy2(db_path, snapshot)
        for suffix in ("-wal", "-shm"):
            side = db_path.with_name(db_path.name + suffix)
            if side.exists():
                shutil.copy2(side, Path(tmp) / side.name)
        conn = sqlite3.connect(str(snapshot))
        conn.execute("PRAGMA query_only=1")  # belt-and-suspenders: forbid writes on the copy
        try:
            yield conn
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Shared projection so every fetch produces rows in the same shape for `_build`.
# Messages in "Recently Deleted" are dropped from chat_message_join but kept (with their
# chat link) in chat_recoverable_message_join, so we union both and flag the deleted ones.
_SELECT = """
    SELECT m.ROWID, m.text, m.attributedBody, m.date, m.is_from_me,
           h.id AS handle, COALESCE(c.display_name, c.chat_identifier) AS chat_name,
           c.ROWID AS chat_id, m.cache_has_attachments,
           COALESCE(cmj.is_deleted, 0) AS is_deleted
    FROM message m
    LEFT JOIN handle h ON m.handle_id = h.ROWID
    LEFT JOIN (
        SELECT message_id, chat_id, MAX(is_deleted) AS is_deleted FROM (
            SELECT message_id, chat_id, 0 AS is_deleted FROM chat_message_join
            UNION ALL
            SELECT message_id, chat_id, 1 AS is_deleted FROM chat_recoverable_message_join
        ) GROUP BY message_id, chat_id
    ) cmj ON cmj.message_id = m.ROWID
    LEFT JOIN chat c ON c.ROWID = cmj.chat_id
"""


def _build(row) -> Message:
    (rowid, text, attr, date_raw, is_from_me, handle, chat_name,
     chat_id, has_attachment, is_deleted) = row
    return Message(
        rowid=rowid,
        text=message_text(text, attr) or "",
        date=apple_time_to_datetime(date_raw),
        is_from_me=bool(is_from_me),
        handle=handle,
        chat_name=chat_name,
        chat_id=chat_id,
        has_attachment=bool(has_attachment),
        date_raw=date_raw,
        is_deleted=bool(is_deleted),
    )


def all_handles(conn: sqlite3.Connection) -> list[str]:
    """Every distinct handle id (phone/email) that appears in the database."""
    return [row[0] for row in conn.execute(
        "SELECT DISTINCT id FROM handle WHERE id IS NOT NULL"
    )]


def attachments_for(
    conn: sqlite3.Connection, rowids: list[int]
) -> dict[int, list[Attachment]]:
    """Attachments for the given message ROWIDs, keyed by message ROWID.

    Batched into one query so rendering a transcript doesn't fan out per message.
    """
    if not rowids:
        return {}
    placeholders = ",".join("?" * len(rowids))
    sql = f"""
        SELECT maj.message_id, a.transfer_name, a.mime_type, a.total_bytes,
               a.filename, a.uti, a.ROWID
        FROM message_attachment_join maj
        JOIN attachment a ON a.ROWID = maj.attachment_id
        WHERE maj.message_id IN ({placeholders})
        ORDER BY a.ROWID
    """
    out: dict[int, list[Attachment]] = {}
    for mid, name, mime, size, path, uti, att_id in conn.execute(sql, rowids):
        out.setdefault(mid, []).append(
            Attachment(name=name, mime_type=mime or uti, total_bytes=size,
                       path=path, rowid=att_id)
        )
    return out


def _handle_filter(handles: list[str] | None, where: list[str], params: list[object]) -> None:
    if handles:
        placeholders = ",".join("?" * len(handles))
        where.append(f"h.id IN ({placeholders})")
        params.extend(handles)


def _deleted_filter(include_deleted: bool, where: list[str]) -> None:
    """By default, hide Recently Deleted messages (keeping rows with no chat link)."""
    if not include_deleted:
        where.append("COALESCE(cmj.is_deleted, 0) = 0")


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    contact: str | None = None,
    handles: list[str] | None = None,
    from_me: bool | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    include_deleted: bool = False,
    limit: int = 50,
) -> list[Message]:
    """Search messages whose text contains ``query`` (case-insensitive substring).

    Because most text is locked inside ``attributedBody``, filtering by content is done
    in Python after decoding. Structural filters (contact, sender, date, limit) run in
    SQL so we only decode a bounded candidate set.
    """
    where = ["1=1"]
    params: list[object] = []

    if contact:
        where.append("h.id LIKE ?")
        params.append(f"%{contact}%")
    _handle_filter(handles, where, params)
    _deleted_filter(include_deleted, where)
    if from_me is not None:
        where.append("m.is_from_me = ?")
        params.append(1 if from_me else 0)
    if since is not None:
        where.append("m.date >= ?")
        params.append(datetime_to_apple_time(since))
    if until is not None:
        where.append("m.date <= ?")
        params.append(datetime_to_apple_time(until))

    # Over-fetch candidates so post-decode text filtering can still return `limit` rows.
    candidate_cap = max(limit * 40, 2000)

    sql = f"{_SELECT} WHERE {' AND '.join(where)} ORDER BY m.date DESC LIMIT ?"
    params.append(candidate_cap)

    needle = query.casefold()
    results: list[Message] = []
    for row in conn.execute(sql, params):
        msg = _build(row)
        if not msg.text or needle not in msg.text.casefold():
            continue
        results.append(msg)
        if len(results) >= limit:
            break
    return results


def transcript(
    conn: sqlite3.Connection,
    *,
    contact: str | None = None,
    chat: str | None = None,
    handles: list[str] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    include_deleted: bool = False,
    limit: int = 1000,
) -> list[Message]:
    """Reconstruct a conversation as an ordered transcript within a timeframe.

    Selects every message (not just text hits) for the matching contact and/or chat,
    ordered chronologically, so the back-and-forth reads as a coherent thread.
    """
    where = ["1=1"]
    params: list[object] = []

    if contact:
        # Match the contact against either the per-message handle (group chats) or the
        # chat identifier (1:1 chats are keyed by the other party's handle).
        where.append("(h.id LIKE ? OR c.chat_identifier LIKE ?)")
        params.extend([f"%{contact}%", f"%{contact}%"])
    _handle_filter(handles, where, params)
    _deleted_filter(include_deleted, where)
    if chat:
        where.append("(c.display_name LIKE ? OR c.chat_identifier LIKE ?)")
        params.extend([f"%{chat}%", f"%{chat}%"])
    if since is not None:
        where.append("m.date >= ?")
        params.append(datetime_to_apple_time(since))
    if until is not None:
        where.append("m.date <= ?")
        params.append(datetime_to_apple_time(until))

    sql = f"{_SELECT} WHERE {' AND '.join(where)} ORDER BY m.date ASC LIMIT ?"
    params.append(limit)
    return [_build(row) for row in conn.execute(sql, params)]


def context_around(
    conn: sqlite3.Connection,
    message: Message,
    *,
    before: int = 3,
    after: int = 3,
) -> list[Message]:
    """Return messages surrounding ``message`` within the same chat, chronologically.

    The result includes ``message`` itself. Ordering is by date then ROWID so ties
    (same-second messages) stay stable.
    """
    if message.chat_id is None or message.date_raw is None:
        return [message]
    anchor = message.date_raw

    prev_sql = (
        f"{_SELECT} WHERE c.ROWID = ? AND (m.date < ? OR (m.date = ? AND m.ROWID < ?)) "
        "ORDER BY m.date DESC, m.ROWID DESC LIMIT ?"
    )
    prev = [_build(r) for r in conn.execute(
        prev_sql, (message.chat_id, anchor, anchor, message.rowid, before)
    )]

    next_sql = (
        f"{_SELECT} WHERE c.ROWID = ? AND (m.date > ? OR (m.date = ? AND m.ROWID > ?)) "
        "ORDER BY m.date ASC, m.ROWID ASC LIMIT ?"
    )
    nxt = [_build(r) for r in conn.execute(
        next_sql, (message.chat_id, anchor, anchor, message.rowid, after)
    )]

    return list(reversed(prev)) + [message] + nxt


def datetime_to_apple_time(dt: datetime) -> int:
    """Convert a datetime to Messages' ns-since-2001 representation."""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int((dt.timestamp() - APPLE_EPOCH_OFFSET) * 1_000_000_000)
