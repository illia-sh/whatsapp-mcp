"""Shared fixtures: a synthetic message store, never real data."""

import sqlite3
from datetime import datetime, timedelta

import pytest

SCHEMA = """
CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
CREATE TABLE messages (
    id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
    is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT,
    media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB, file_length INTEGER,
    PRIMARY KEY (id, chat_jid)
);
"""

ALICE = "15550000001@s.whatsapp.net"
GROUP = "120363000000000001@g.us"


@pytest.fixture
def message_db(tmp_path, monkeypatch):
    """A small store with one direct chat and one group, pointed to by whatsapp.py."""
    path = tmp_path / "messages.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    t0 = datetime(2026, 1, 1, 9, 0, 0)
    rows = [
        ("m1", ALICE, "15550000001", "hello", t0, False, None),
        ("m2", ALICE, "15550000000", "hi back", t0 + timedelta(minutes=1), True, None),
        ("m3", GROUP, "15550000002", "group note", t0 + timedelta(minutes=2), False, None),
        ("m4", GROUP, "15550000001", "", t0 + timedelta(minutes=3), False, "image"),
    ]
    for mid, chat, sender, content, ts, me, media in rows:
        db.execute(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (mid, chat, sender, content, ts, me, media))
    db.execute("INSERT INTO chats VALUES (?, ?, ?)", (ALICE, "Alice", t0 + timedelta(minutes=1)))
    db.execute("INSERT INTO chats VALUES (?, ?, ?)", (GROUP, "Test Group", t0 + timedelta(minutes=3)))
    db.commit()
    db.close()

    import whatsapp
    monkeypatch.setattr(whatsapp, "MESSAGES_DB_PATH", str(path))
    return path
