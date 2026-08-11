"""Resolve iMessage handles (phone/email) to contact names from the macOS AddressBook.

Contacts live in one or more ``AddressBook-v22.abcddb`` SQLite databases under
``~/Library/Application Support/AddressBook`` (a top-level store plus per-source stores).
All are opened strictly read-only.

Phone numbers are stored in many formats — ``5555550142``, ``(555) 555-0134``,
``+86 138-0000-0000`` — so we match on digits only, keyed by the last 10 (enough to
disambiguate personal contacts while ignoring country-code/formatting differences).
Emails match directly, case-insensitively.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .db import open_readonly

DEFAULT_ADDRESSBOOK_DIR = Path.home() / "Library" / "Application Support" / "AddressBook"


def _digits_key(value: str) -> str | None:
    """Last 10 digits of a phone-like string, or None if too few digits."""
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else None


def _display_name(first, last, org, nickname) -> str | None:
    if nickname:
        return nickname
    name = " ".join(part for part in (first, last) if part).strip()
    return name or (org or None)


def find_addressbook_dbs(base: Path = DEFAULT_ADDRESSBOOK_DIR) -> list[Path]:
    """All AddressBook databases: the top-level store plus every per-source store."""
    dbs: list[Path] = []
    top = base / "AddressBook-v22.abcddb"
    if top.exists():
        dbs.append(top)
    dbs.extend(sorted((base / "Sources").glob("*/AddressBook-v22.abcddb")))
    return dbs


@dataclass
class ContactBook:
    """Maps normalized phone/email keys to contact display names."""

    by_phone: dict[str, str] = field(default_factory=dict)
    by_email: dict[str, str] = field(default_factory=dict)

    def name_for(self, handle: str | None) -> str | None:
        """Return a contact name for an iMessage handle, or None if unknown."""
        if not handle:
            return None
        if "@" in handle:
            return self.by_email.get(handle.strip().casefold())
        key = _digits_key(handle)
        return self.by_phone.get(key) if key else None

    def label(self, handle: str | None) -> str:
        """Name if known, otherwise the raw handle (or ``?``)."""
        return self.name_for(handle) or handle or "?"


def _load_one(book: ContactBook, db_path: Path) -> None:
    try:
        with open_readonly(db_path) as conn:
            phones = conn.execute(
                """
                SELECT p.ZFULLNUMBER, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION, r.ZNICKNAME
                FROM ZABCDPHONENUMBER p JOIN ZABCDRECORD r ON p.ZOWNER = r.Z_PK
                WHERE p.ZFULLNUMBER IS NOT NULL
                """
            ).fetchall()
            for number, first, last, org, nickname in phones:
                key = _digits_key(number)
                name = _display_name(first, last, org, nickname)
                # First writer wins so the primary store isn't overwritten by a duplicate.
                if key and name:
                    book.by_phone.setdefault(key, name)

            emails = conn.execute(
                """
                SELECT e.ZADDRESS, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION, r.ZNICKNAME
                FROM ZABCDEMAILADDRESS e JOIN ZABCDRECORD r ON e.ZOWNER = r.Z_PK
                WHERE e.ZADDRESS IS NOT NULL
                """
            ).fetchall()
            for address, first, last, org, nickname in emails:
                name = _display_name(first, last, org, nickname)
                if name:
                    book.by_email.setdefault(address.strip().casefold(), name)
    except (FileNotFoundError, sqlite3.Error):
        # A malformed/locked/missing source shouldn't break name resolution entirely.
        pass


def load_contacts(base: Path = DEFAULT_ADDRESSBOOK_DIR) -> ContactBook:
    """Build a ContactBook from every available AddressBook database.

    Never raises on missing/inaccessible databases — returns whatever it could read
    (an empty book if the AddressBook is unavailable), so callers can degrade to raw
    handles rather than failing.
    """
    book = ContactBook()
    for db_path in find_addressbook_dbs(base):
        _load_one(book, db_path)
    return book
