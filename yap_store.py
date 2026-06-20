"""
YAP — anonymous relay DM threads.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from config import DB_DIR

from tz_utils import now_sgt

DB_PATH = Path(DB_DIR) / "confessions.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_yap_db() -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS yap_threads (
                thread_id           INTEGER PRIMARY KEY AUTOINCREMENT,
                reader_id           INTEGER NOT NULL,
                poster_id           INTEGER NOT NULL,
                confession_message_id INTEGER NOT NULL,
                created_at          TEXT NOT NULL,
                UNIQUE(reader_id, poster_id, confession_message_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS yap_active_thread (
                user_id     INTEGER PRIMARY KEY,
                thread_id   INTEGER NOT NULL
            )
            """
        )
        conn.commit()


def get_or_create_thread(reader_id: int, poster_id: int, confession_message_id: int) -> int:
    with closing(_connect()) as conn:
        existing = conn.execute(
            """
            SELECT thread_id FROM yap_threads
            WHERE reader_id = ? AND poster_id = ? AND confession_message_id = ?
            """,
            (reader_id, poster_id, confession_message_id),
        ).fetchone()
        if existing:
            return existing["thread_id"]

        cur = conn.execute(
            """
            INSERT INTO yap_threads (reader_id, poster_id, confession_message_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (reader_id, poster_id, confession_message_id, now_sgt().isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def get_thread(thread_id: int):
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT * FROM yap_threads WHERE thread_id = ?", (thread_id,)
        )
        return cur.fetchone()


def set_active_thread(user_id: int, thread_id: int) -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            INSERT INTO yap_active_thread (user_id, thread_id) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET thread_id = excluded.thread_id
            """,
            (user_id, thread_id),
        )
        conn.commit()


def get_active_thread_id(user_id: int):
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT thread_id FROM yap_active_thread WHERE user_id = ?", (user_id,)
        )
        row = cur.fetchone()
        return row["thread_id"] if row else None


def clear_active_thread(user_id: int) -> None:
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM yap_active_thread WHERE user_id = ?", (user_id,))
        conn.commit()


def get_other_party(thread_id: int, user_id: int):
    thread = get_thread(thread_id)
    if thread is None:
        return None
    if thread["reader_id"] == user_id:
        return thread["poster_id"]
    if thread["poster_id"] == user_id:
        return thread["reader_id"]
    return None
