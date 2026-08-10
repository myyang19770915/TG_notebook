"""Per-chat state: active notebook, conversation continuity, pending jobs."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "state.db"
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_state (
    chat_id           INTEGER PRIMARY KEY,
    notebook_id       TEXT,
    notebook_title    TEXT,
    conversation_id   TEXT,
    updated_at        REAL
);
CREATE TABLE IF NOT EXISTS jobs (
    artifact_id   TEXT,
    chat_id       INTEGER,
    notebook_id   TEXT,
    kind          TEXT,
    label         TEXT,
    status_msg_id INTEGER,
    created_at    REAL,
    done          INTEGER DEFAULT 0,
    PRIMARY KEY (artifact_id, chat_id)
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _lock, _conn() as c:
        c.executescript(_SCHEMA)


# ------------------------------------------------------------- active notebook

def set_active(chat_id: int, notebook_id: str, title: str) -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO chat_state (chat_id, notebook_id, notebook_title, conversation_id, updated_at)"
            " VALUES (?,?,?,NULL,?)"
            " ON CONFLICT(chat_id) DO UPDATE SET notebook_id=excluded.notebook_id,"
            " notebook_title=excluded.notebook_title, conversation_id=NULL,"
            " updated_at=excluded.updated_at",
            (chat_id, notebook_id, title, time.time()),
        )


def get_active(chat_id: int) -> sqlite3.Row | None:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM chat_state WHERE chat_id=? AND notebook_id IS NOT NULL", (chat_id,)
        ).fetchone()
    return row


def clear_active(chat_id: int) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM chat_state WHERE chat_id=?", (chat_id,))


def set_conversation(chat_id: int, conversation_id: str | None) -> None:
    with _lock, _conn() as c:
        c.execute(
            "UPDATE chat_state SET conversation_id=? WHERE chat_id=?", (conversation_id, chat_id)
        )


# ------------------------------------------------------------------ jobs

def add_job(artifact_id: str, chat_id: int, notebook_id: str, kind: str,
            label: str, status_msg_id: int | None) -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO jobs"
            " (artifact_id, chat_id, notebook_id, kind, label, status_msg_id, created_at, done)"
            " VALUES (?,?,?,?,?,?,?,0)",
            (artifact_id, chat_id, notebook_id, kind, label, status_msg_id, time.time()),
        )


def pending_jobs() -> list[sqlite3.Row]:
    with _lock, _conn() as c:
        return c.execute("SELECT * FROM jobs WHERE done=0 ORDER BY created_at").fetchall()


def finish_job(artifact_id: str, chat_id: int) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE jobs SET done=1 WHERE artifact_id=? AND chat_id=?",
                  (artifact_id, chat_id))
