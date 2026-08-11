"""Tests for AddressBook name resolution against a synthetic contacts db."""

import sqlite3

import pytest

from imsearch import contacts


def test_digits_key_takes_last_10():
    assert contacts._digits_key("+1 (555) 555-0142") == "5555550142"
    assert contacts._digits_key("(555) 555-0142") == "5555550142"
    # International: last 10 digits are used so formatting/country code don't block a match.
    assert contacts._digits_key("+86 138-0000-0000") == "3800000000"
    assert contacts._digits_key("911") is None  # too few digits


def test_display_name_precedence():
    assert contacts._display_name("Jane", "Doe", None, None) == "Jane Doe"
    assert contacts._display_name(None, None, "Acme Inc", None) == "Acme Inc"
    assert contacts._display_name("Bob", None, "Acme", "Bobby") == "Bobby"  # nickname wins
    assert contacts._display_name(None, None, None, None) is None


@pytest.fixture
def book(tmp_path):
    db_path = tmp_path / "Sources" / "X" / "AddressBook-v22.abcddb"
    db_path.parent.mkdir(parents=True)
    c = sqlite3.connect(db_path)
    c.executescript(
        """
        CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT,
            ZLASTNAME TEXT, ZORGANIZATION TEXT, ZNICKNAME TEXT);
        CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT);
        CREATE TABLE ZABCDEMAILADDRESS (ZOWNER INTEGER, ZADDRESS TEXT);
        """
    )
    c.execute("INSERT INTO ZABCDRECORD VALUES (1,'Jane','Doe',NULL,NULL)")
    c.execute("INSERT INTO ZABCDPHONENUMBER VALUES (1,'+1 (555) 555-0142')")
    c.execute("INSERT INTO ZABCDEMAILADDRESS VALUES (1,'Jane.Doe@EXAMPLE.com')")
    c.commit()
    c.close()
    return contacts.load_contacts(tmp_path)


def test_name_for_phone_ignores_formatting(book):
    assert book.name_for("+15555550142") == "Jane Doe"
    assert book.name_for("5555550142") == "Jane Doe"


def test_name_for_email_is_case_insensitive(book):
    assert book.name_for("jane.doe@example.com") == "Jane Doe"


def test_label_falls_back_to_raw_handle(book):
    assert book.label("+15550000000") == "+15550000000"
    assert book.label(None) == "?"


def test_load_contacts_missing_dir_is_empty(tmp_path):
    empty = contacts.load_contacts(tmp_path / "nonexistent")
    assert empty.by_phone == {} and empty.by_email == {}
