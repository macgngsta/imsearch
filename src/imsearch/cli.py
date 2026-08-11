"""Command-line interface for searching iMessage history (read-only)."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import contacts, db


def _parse_date(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; use ISO format like 2025-01-31 or 2025-01-31T14:00"
        )


_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_DURATION_RE = re.compile(r"(?i)^(\d+)\s*([smhdw])$")


def _parse_duration(value: str) -> timedelta:
    """Parse a relative window like ``3h``, ``90m``, ``2d``, ``1w`` into a timedelta."""
    match = _DURATION_RE.match(value.strip())
    if not match:
        raise argparse.ArgumentTypeError(
            f"invalid duration {value!r}; use e.g. 30m, 3h, 2d, 1w"
        )
    amount, unit = int(match.group(1)), match.group(2).lower()
    return timedelta(**{_DURATION_UNITS[unit]: amount})


def _add_timeframe_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--since", type=_parse_date, help="only messages on/after this date (ISO)")
    p.add_argument("--until", type=_parse_date, help="only messages on/before this date (ISO)")
    p.add_argument(
        "--last",
        type=_parse_duration,
        metavar="DURATION",
        help="relative window ending now, e.g. 3h, 90m, 2d, 1w (sets --since)",
    )
    p.add_argument(
        "--no-names",
        action="store_true",
        help="don't resolve phone/email handles to contact names",
    )
    p.add_argument(
        "--db",
        type=str,
        default=None,
        help=f"path to chat.db (default: {db.DEFAULT_DB_PATH})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="imsearch",
        description="Search and reconstruct your local iMessage history (read-only).",
    )
    sub = parser.add_subparsers(dest="command")

    # search
    s = sub.add_parser("search", help="find messages containing text")
    s.add_argument("query", help="text to search for (case-insensitive substring)")
    s.add_argument("--contact", help="filter by the other party's phone/email (substring)")
    s.add_argument("--name", help="filter by contact name (resolved via AddressBook)")
    sender = s.add_mutually_exclusive_group()
    sender.add_argument("--from-me", action="store_true", help="only messages you sent")
    sender.add_argument("--from-them", action="store_true", help="only messages you received")
    s.add_argument(
        "--context",
        type=int,
        default=0,
        metavar="N",
        help="show N messages before and after each hit, in conversation order",
    )
    s.add_argument(
        "--deleted",
        action="store_true",
        help="also include messages in the 'Recently Deleted' store",
    )
    s.add_argument(
        "--open-attachments",
        action="store_true",
        help="open matched messages' attachment files (macOS `open`)",
    )
    s.add_argument("--limit", type=int, default=50, help="max results (default: 50)")
    _add_timeframe_args(s)
    s.set_defaults(func=cmd_search)

    # thread
    t = sub.add_parser(
        "thread",
        help="reconstruct a conversation transcript within a timeframe",
    )
    t.add_argument("--contact", help="phone/email of the conversation (substring)")
    t.add_argument("--name", help="contact name to reconstruct (resolved via AddressBook)")
    t.add_argument("--chat", help="group name or chat identifier (substring)")
    t.add_argument(
        "--deleted",
        action="store_true",
        help="also include messages in the 'Recently Deleted' store (marked with *)",
    )
    t.add_argument(
        "--open-attachments",
        action="store_true",
        help="open the transcript's attachment files (macOS `open`)",
    )
    t.add_argument("--limit", type=int, default=1000, help="max messages (default: 1000)")
    _add_timeframe_args(t)
    t.set_defaults(func=cmd_thread)

    # serve
    w = sub.add_parser("serve", help="launch the local web UI (search + inline images)")
    w.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    w.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    w.set_defaults(func=None)  # handled directly in main(); needs no shared connection

    return parser


def cmd_serve(args, console: Console) -> int:
    import uvicorn  # imported lazily so non-web commands don't pay the import cost

    from . import web

    console.print(
        f"[green]imsearch web UI[/green] → [bold]http://{args.host}:{args.port}[/bold]  "
        "[dim](Ctrl+C to stop)[/dim]"
    )
    uvicorn.run(web.create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


def _open(console: Console, args, stack: ExitStack):
    """Enter a read-only connection into ``stack``, or report the error and return None."""
    db_path = db.Path(args.db) if args.db else db.DEFAULT_DB_PATH
    try:
        return stack.enter_context(db.open_readonly(db_path))
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
    except Exception as exc:  # noqa: BLE001 - surface DB/permission errors clearly
        console.print(f"[red]Error reading database:[/red] {exc}")
        console.print(
            "[yellow]If this is a permissions error, grant your terminal "
            "Full Disk Access in System Settings → Privacy & Security.[/yellow]"
        )
    return None


def _resolve_handles(conn, book: contacts.ContactBook, name: str) -> list[str]:
    """Handles whose contact name contains ``name`` (case-insensitive)."""
    needle = name.casefold()
    return [h for h in db.all_handles(conn)
            if needle in (book.name_for(h) or "").casefold()]


_ATTACH_INDENT = " " * 36  # aligns attachment lines under the message body


def _human_size(n: int | None) -> str:
    if not n:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _att_label(att: db.Attachment) -> str:
    name = att.name or (att.path.rsplit("/", 1)[-1] if att.path else "attachment")
    parts = [name]
    if att.mime_type:
        parts.append(att.mime_type)
    parts.append(_human_size(att.total_bytes))
    return " · ".join(parts)


def _render_line(
    console: Console,
    msg: db.Message,
    book: contacts.ContactBook,
    *,
    highlight: bool = False,
    attachments: list[db.Attachment] | None = None,
) -> None:
    when = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "?"
    who = "me" if msg.is_from_me else book.label(msg.handle)
    # A trailing '*' on the name flags a message from the "Recently Deleted" store.
    who = f"{who}*" if msg.is_deleted else who
    if msg.text:
        raw = msg.text
    elif attachments:
        raw = ""  # the attachment lines below carry the content
    else:
        raw = "[attachment]" if msg.has_attachment else "[no text]"
    body = escape(raw)
    who_style = "bold red" if msg.is_deleted else ("bold cyan" if msg.is_from_me else "bold green")
    marker = "[yellow]›[/yellow] " if highlight else "  "
    body_markup = f"[bold]{body}[/bold]" if highlight else body
    console.print(
        f"{marker}[dim]{when}[/dim]  [{who_style}]{who:>14}[/{who_style}]  {body_markup}"
    )
    for att in attachments or []:
        console.print(f"{_ATTACH_INDENT}[dim]📎 {escape(_att_label(att))}[/dim]")
        resolved = att.resolved_path()
        if resolved:
            exists = resolved.exists()
            style = "dim" if exists else "dim red"
            note = "" if exists else "  (file not on disk)"
            console.print(f"{_ATTACH_INDENT}[{style}]{escape(str(resolved))}{note}[/{style}]")


def _open_attachments(console: Console, atts_by_msg: dict[int, list[db.Attachment]]) -> None:
    """Open every attachment file with the macOS `open` command."""
    paths = [
        str(p)
        for atts in atts_by_msg.values()
        for att in atts
        if (p := att.resolved_path()) and p.exists()
    ]
    if not paths:
        console.print("[yellow]No attachment files found on disk to open.[/yellow]")
        return
    console.print(f"[dim]Opening {len(paths)} attachment(s)…[/dim]")
    subprocess.run(["open", *paths], check=False)


def cmd_search(args, console: Console, conn, book: contacts.ContactBook) -> int:
    from_me: bool | None = True if args.from_me else (False if args.from_them else None)
    display = contacts.ContactBook() if args.no_names else book
    handles = None
    if args.name:
        handles = _resolve_handles(conn, book, args.name)
        if not handles:
            console.print(f"[yellow]No contact matching[/yellow] {args.name!r}")
            return 0
    results = db.search(
        conn,
        args.query,
        contact=args.contact,
        handles=handles,
        from_me=from_me,
        since=args.since,
        until=args.until,
        include_deleted=args.deleted,
        limit=args.limit,
    )
    if not results:
        console.print(f"[yellow]No messages found matching[/yellow] {args.query!r}")
        return 0

    match_atts = db.attachments_for(conn, [m.rowid for m in results if m.has_attachment])

    if args.context > 0:
        console.print(f"[bold]{len(results)} match(es) for {args.query!r}[/bold] "
                      f"(± {args.context} for context)\n")
        for i, msg in enumerate(results):
            chat = msg.chat_name or display.label(msg.handle)
            console.print(f"[dim]— in {escape(chat)} —[/dim]")
            window = db.context_around(conn, msg, before=args.context, after=args.context)
            atts = db.attachments_for(conn, [m.rowid for m in window if m.has_attachment])
            for ctx in window:
                _render_line(console, ctx, display, highlight=(ctx.rowid == msg.rowid),
                             attachments=atts.get(ctx.rowid))
            if i < len(results) - 1:
                console.print()
        if args.open_attachments:
            _open_attachments(console, match_atts)
        return 0

    table = Table(title=f"{len(results)} match(es) for {args.query!r}")
    table.add_column("Date", style="cyan", no_wrap=True)
    table.add_column("Who", style="magenta", no_wrap=True)
    table.add_column("Contact", style="green", no_wrap=True)
    table.add_column("Message", overflow="fold")
    for msg in results:
        when = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "?"
        who = "me" if msg.is_from_me else "them"
        contact = display.name_for(msg.handle) or msg.chat_name or msg.handle or "?"
        contact = f"{contact}*" if msg.is_deleted else contact  # * marks Recently Deleted
        text = msg.text or ("[attachment]" if msg.has_attachment else "")
        table.add_row(when, who, escape(contact), escape(text))
    console.print(table)
    if args.open_attachments:
        _open_attachments(console, match_atts)
    return 0


def cmd_thread(args, console: Console, conn, book: contacts.ContactBook) -> int:
    if not (args.contact or args.chat or args.name):
        console.print("[red]thread requires --contact, --name, or --chat[/red]")
        return 2
    display = contacts.ContactBook() if args.no_names else book
    handles = None
    if args.name:
        handles = _resolve_handles(conn, book, args.name)
        if not handles:
            console.print(f"[yellow]No contact matching[/yellow] {args.name!r}")
            return 0
    messages = db.transcript(
        conn,
        contact=args.contact,
        chat=args.chat,
        handles=handles,
        since=args.since,
        until=args.until,
        include_deleted=args.deleted,
        limit=args.limit,
    )
    if not messages:
        console.print("[yellow]No messages found for that conversation/timeframe.[/yellow]")
        return 0

    who = args.name or args.contact or args.chat
    span = ""
    if messages[0].date and messages[-1].date:
        span = f" — {messages[0].date:%Y-%m-%d %H:%M} to {messages[-1].date:%Y-%m-%d %H:%M}"
    console.print(f"[bold]Transcript: {escape(who)}{span}[/bold]  ({len(messages)} messages)\n")
    atts = db.attachments_for(conn, [m.rowid for m in messages if m.has_attachment])
    for msg in messages:
        _render_line(console, msg, display, attachments=atts.get(msg.rowid))
    if args.open_attachments:
        _open_attachments(console, atts)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    if getattr(args, "last", None) is not None:
        if args.since is not None:
            parser.error("--last and --since are mutually exclusive")
        args.since = datetime.now().astimezone() - args.last

    console = Console()

    # `serve` runs a web server that manages its own per-request connections.
    if args.command == "serve":
        return cmd_serve(args, console)

    with ExitStack() as stack:
        conn = _open(console, args, stack)
        if conn is None:
            return 1
        # Always load contacts (needed to resolve --name); --no-names only hides them at
        # display time, handled per-command via the display book.
        book = contacts.load_contacts()
        return args.func(args, console, conn, book)


if __name__ == "__main__":
    sys.exit(main())
