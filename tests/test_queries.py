"""Tests for search / transcript / context_around against a synthetic chat.db.

Building a tiny in-memory database with the columns imsearch reads lets us pin the
query logic (ordering, filters, context windowing) without a real Messages database.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

from imsearch import db


def _apple(dt: datetime) -> int:
    return db.datetime_to_apple_time(dt.replace(tzinfo=timezone.utc))


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, display_name TEXT, chat_identifier TEXT);
        CREATE TABLE chat_message_join (message_id INTEGER, chat_id INTEGER);
        CREATE TABLE chat_recoverable_message_join (message_id INTEGER, chat_id INTEGER);
        CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, transfer_name TEXT,
            mime_type TEXT, total_bytes INTEGER, filename TEXT, uti TEXT);
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB, date INTEGER,
            is_from_me INTEGER, handle_id INTEGER, cache_has_attachments INTEGER DEFAULT 0
        );
        """
    )
    c.execute("INSERT INTO handle VALUES (1, '+15551234567')")
    c.execute("INSERT INTO chat VALUES (1, 'Lunch Crew', 'chat-abc')")
    # Five chronological messages in one chat; message 3 is the search target.
    rows = [
        (1, "hey are you around", 0),
        (2, "yeah what's up", 1),
        (3, "want to grab dinner tonight", 0),
        (4, "sounds great", 1),
        (5, "see you at 7", 0),
    ]
    base = datetime(2025, 3, 1, 18, 0)
    for i, (rid, text, from_me) in enumerate(rows):
        dt = base.replace(minute=i)
        c.execute(
            "INSERT INTO message (ROWID, text, date, is_from_me, handle_id) VALUES (?,?,?,?,?)",
            (rid, text, _apple(dt), from_me, None if from_me else 1),
        )
        c.execute("INSERT INTO chat_message_join VALUES (?, 1)", (rid,))

    # Message 6: a "Recently Deleted" message (in the recoverable join, not chat_message_join).
    c.execute(
        "INSERT INTO message (ROWID, text, date, is_from_me, handle_id) VALUES (?,?,?,?,?)",
        (6, "oops deleted dinner plan", _apple(base.replace(minute=6)), 0, 1),
    )
    c.execute("INSERT INTO chat_recoverable_message_join VALUES (6, 1)")

    # Message 3 also carries an attachment.
    c.execute(
        "INSERT INTO attachment (ROWID, transfer_name, mime_type, total_bytes, filename, uti) "
        "VALUES (10, 'IMG_1.jpg', 'image/jpeg', 2048, '~/att/IMG_1.jpg', 'public.jpeg')"
    )
    c.execute("INSERT INTO message_attachment_join VALUES (3, 10)")
    c.commit()
    return c


def test_search_matches_text(conn):
    results = db.search(conn, "dinner")
    assert [m.rowid for m in results] == [3]
    assert results[0].chat_name == "Lunch Crew"


def test_search_is_case_insensitive(conn):
    assert db.search(conn, "DINNER")


def test_search_from_me_filter(conn):
    # "see you" and "yeah"/"sounds great" — only from-me messages.
    results = db.search(conn, "you", from_me=True)
    assert all(m.is_from_me for m in results)


def test_transcript_is_chronological(conn):
    msgs = db.transcript(conn, chat="Lunch")
    assert [m.rowid for m in msgs] == [1, 2, 3, 4, 5]


def test_transcript_timeframe_bounds(conn):
    since = datetime(2025, 3, 1, 18, 2, tzinfo=timezone.utc)
    until = datetime(2025, 3, 1, 18, 3, tzinfo=timezone.utc)
    msgs = db.transcript(conn, chat="Lunch", since=since, until=until)
    assert [m.rowid for m in msgs] == [3, 4]


def test_context_around_windows_and_includes_match(conn):
    target = db.search(conn, "dinner")[0]
    ctx = db.context_around(conn, target, before=1, after=1)
    assert [m.rowid for m in ctx] == [2, 3, 4]
    # The match must appear exactly once (regression: float round-trip duplicated it).
    assert sum(m.rowid == target.rowid for m in ctx) == 1


def test_context_clamps_at_conversation_start(conn):
    first = db.transcript(conn, chat="Lunch")[0]
    ctx = db.context_around(conn, first, before=5, after=1)
    assert [m.rowid for m in ctx] == [1, 2]


def test_deleted_excluded_by_default(conn):
    msgs = db.transcript(conn, chat="Lunch")
    assert 6 not in [m.rowid for m in msgs]
    assert all(not m.is_deleted for m in msgs)


def test_deleted_included_when_requested(conn):
    msgs = db.transcript(conn, chat="Lunch", include_deleted=True)
    deleted = [m for m in msgs if m.rowid == 6]
    assert deleted and deleted[0].is_deleted
    # It slots into chronological order like any other message.
    assert [m.rowid for m in msgs] == [1, 2, 3, 4, 5, 6]


def test_search_excludes_deleted_unless_requested(conn):
    assert db.search(conn, "deleted") == []
    hit = db.search(conn, "deleted", include_deleted=True)
    assert [m.rowid for m in hit] == [6] and hit[0].is_deleted


def test_attachments_for_batches_by_message(conn):
    atts = db.attachments_for(conn, [1, 2, 3])
    assert set(atts) == {3}
    (att,) = atts[3]
    assert att.name == "IMG_1.jpg"
    assert att.mime_type == "image/jpeg"
    assert att.total_bytes == 2048


def test_attachments_for_empty_input(conn):
    assert db.attachments_for(conn, []) == {}
