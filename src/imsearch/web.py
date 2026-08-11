"""Local web UI for imsearch: a search form that renders messages and inline images.

Runs a small FastAPI server on localhost that reuses the read-only query layer. Nothing
is written to chat.db. Attachment files are served by integer id and confined to the
Messages Attachments directory (see :func:`imsearch.db.attachment_path`).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import contacts, db

STATIC_DIR = Path(__file__).parent / "static"


def _serialize(msg: db.Message, atts: list[db.Attachment], book: contacts.ContactBook) -> dict:
    return {
        "rowid": msg.rowid,
        "date": msg.date.isoformat() if msg.date else None,
        "is_from_me": msg.is_from_me,
        "sender": "me" if msg.is_from_me else book.label(msg.handle),
        "text": msg.text,
        "is_deleted": msg.is_deleted,
        "attachments": [
            {
                "id": a.rowid,
                "name": a.name or (a.path.rsplit("/", 1)[-1] if a.path else "attachment"),
                "mime": a.mime_type,
                "size": a.total_bytes,
                "is_image": a.is_image,
                "url": f"/attachment/{a.rowid}" if a.rowid is not None else None,
            }
            for a in atts
        ],
    }


def create_app(
    db_path: Path = db.DEFAULT_DB_PATH,
    addressbook_dir: Path = contacts.DEFAULT_ADDRESSBOOK_DIR,
) -> FastAPI:
    app = FastAPI(title="imsearch", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # Contacts change rarely; load once at startup so every request doesn't re-read them.
    book = contacts.load_contacts(addressbook_dir)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC_DIR / "index.html").read_text()

    @app.get("/api/search")
    def api_search(
        q: str = "",
        name: str = "",
        contact: str = "",
        last: str = "",
        deleted: bool = False,
        images_only: bool = False,
        limit: int = Query(200, ge=1, le=1000),
    ) -> dict:
        with db.open_readonly(db_path) as conn:
            handles = None
            if name:
                needle = name.casefold()
                handles = [h for h in db.all_handles(conn)
                           if needle in (book.name_for(h) or "").casefold()]
                if not handles:
                    return {"messages": [], "note": f"No contact matching {name!r}"}

            since = None
            if last:
                from .cli import _parse_duration  # shared duration grammar (3h, 2d, 1w)
                try:
                    since = datetime.now().astimezone() - _parse_duration(last)
                except Exception:
                    raise HTTPException(400, f"invalid duration: {last!r}")

            results = db.search(
                conn, q, contact=contact or None, handles=handles,
                since=since, include_deleted=deleted, limit=limit,
            )
            atts = db.attachments_for(conn, [m.rowid for m in results if m.has_attachment])
            messages = [_serialize(m, atts.get(m.rowid, []), book) for m in results]

        if images_only:
            messages = [m for m in messages if any(a["is_image"] for a in m["attachments"])]
        return {"messages": messages}

    @app.get("/attachment/{attachment_id}")
    def attachment(attachment_id: int) -> FileResponse:
        with db.open_readonly(db_path) as conn:
            path = db.attachment_path(conn, attachment_id)
        if path is None:
            raise HTTPException(404, "attachment not found")
        return FileResponse(path)

    return app
