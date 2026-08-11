"""Tests for the web layer: search API, attachment serving, and path-traversal safety."""

import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from imsearch import db, web

SCHEMA = """
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


def _apple(dt: datetime) -> int:
    return db.datetime_to_apple_time(dt.replace(tzinfo=timezone.utc))


@pytest.fixture
def client(tmp_path, monkeypatch):
    # A fake Attachments root with one real image file inside it.
    root = tmp_path / "Attachments"
    root.mkdir()
    img = root / "cat.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0JPEGBYTES")
    monkeypatch.setattr(db, "ATTACHMENTS_ROOT", root.resolve())

    db_path = tmp_path / "chat.db"
    c = sqlite3.connect(db_path)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO handle VALUES (1, '+15551230000')")
    c.execute(
        "INSERT INTO message (ROWID, text, date, is_from_me, handle_id, cache_has_attachments) "
        "VALUES (1, 'hello world', ?, 0, 1, 1)",
        (_apple(datetime(2025, 5, 1, 12, 0)),),
    )
    c.execute(
        "INSERT INTO attachment (ROWID, transfer_name, mime_type, total_bytes, filename, uti) "
        "VALUES (7, 'cat.jpg', 'image/jpeg', 9, ?, 'public.jpeg')",
        (str(img),),
    )
    c.execute("INSERT INTO message_attachment_join VALUES (1, 7)")
    c.commit()
    c.close()

    addressbook = tmp_path / "AddressBook"
    addressbook.mkdir()
    app = web.create_app(db_path=db_path, addressbook_dir=addressbook)
    return TestClient(app), img


def test_index_served(client):
    tc, _ = client
    r = tc.get("/")
    assert r.status_code == 200 and "imsearch" in r.text


def test_search_returns_message_with_image(client):
    tc, _ = client
    r = tc.get("/api/search", params={"q": "hello"})
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["text"] == "hello world"
    att = msgs[0]["attachments"][0]
    assert att["is_image"] and att["url"] == "/attachment/7"


def test_images_only_filter(client):
    tc, _ = client
    # The one message has an image, so it survives images_only.
    assert tc.get("/api/search", params={"q": "hello", "images_only": "true"}).json()["messages"]


def test_attachment_is_served(client):
    tc, img = client
    r = tc.get("/attachment/7")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert r.content == img.read_bytes()


def test_attachment_bad_id_404(client):
    tc, _ = client
    assert tc.get("/attachment/99999").status_code == 404


def test_search_unknown_name_returns_note(client):
    tc, _ = client
    data = tc.get("/api/search", params={"name": "nobody here"}).json()
    assert data["messages"] == [] and "note" in data


def test_attachment_path_blocks_traversal(tmp_path, monkeypatch):
    root = tmp_path / "Attachments"
    root.mkdir()
    inside = root / "ok.jpg"
    inside.write_bytes(b"x")
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"secret")
    monkeypatch.setattr(db, "ATTACHMENTS_ROOT", root.resolve())

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT)")
    conn.executemany(
        "INSERT INTO attachment VALUES (?, ?)",
        [
            (1, str(inside)),
            (2, str(outside)),                       # sibling of the root
            (3, str(root / ".." / "secret.txt")),    # traversal out of the root
            (4, str(root / "missing.jpg")),          # inside root but no file
        ],
    )
    assert db.attachment_path(conn, 1) == inside.resolve()
    assert db.attachment_path(conn, 2) is None
    assert db.attachment_path(conn, 3) is None
    assert db.attachment_path(conn, 4) is None
    assert db.attachment_path(conn, 999) is None
