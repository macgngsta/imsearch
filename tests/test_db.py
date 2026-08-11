"""Unit tests for the pure, deterministic helpers in imsearch.db.

The DB-touching code is intentionally not tested here — it requires a real chat.db
and Full Disk Access. These tests pin the conversions that are easy to get wrong.
"""

from datetime import datetime, timedelta, timezone

import pytest

from imsearch import cli, db


def test_apple_time_roundtrip():
    dt = datetime(2025, 1, 31, 14, 30, tzinfo=timezone.utc)
    raw = db.datetime_to_apple_time(dt)
    # Round-trips back to the same instant (compare in UTC to ignore local tz display).
    assert db.apple_time_to_datetime(raw).astimezone(timezone.utc) == dt


def test_apple_time_none_and_zero():
    assert db.apple_time_to_datetime(None) is None
    assert db.apple_time_to_datetime(0) is None


def test_apple_epoch_reference():
    # 0 ns after the Apple epoch is 2001-01-01 00:00:00 UTC.
    assert db.apple_time_to_datetime(1).astimezone(timezone.utc).year == 2001


def test_message_text_prefers_plain_text():
    # When the text column is populated we never touch the blob.
    assert db.message_text("hello", b"ignored-blob") == "hello"


def test_message_text_empty_falls_back_to_blob():
    # Empty text falls through to attributedBody decoding (None blob -> None).
    assert db.message_text("", None) is None
    assert db.message_text(None, None) is None


def test_decode_attributed_body_handles_garbage():
    # Malformed blobs must not raise, just yield None.
    assert db.decode_attributed_body(b"not a real typedstream") is None
    assert db.decode_attributed_body(None) is None


def test_parse_duration_units():
    assert cli._parse_duration("30s") == timedelta(seconds=30)
    assert cli._parse_duration("90m") == timedelta(minutes=90)
    assert cli._parse_duration("3h") == timedelta(hours=3)
    assert cli._parse_duration("2d") == timedelta(days=2)
    assert cli._parse_duration("1w") == timedelta(weeks=1)
    assert cli._parse_duration("3H") == timedelta(hours=3)  # case-insensitive


def test_parse_duration_rejects_junk():
    import argparse
    for bad in ("3", "h", "3 hours", "-3h", ""):
        with pytest.raises(argparse.ArgumentTypeError):
            cli._parse_duration(bad)
