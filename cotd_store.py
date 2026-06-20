"""
Confession of the Day.
"""

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).parent / "confessions.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_cotd_db() -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_confessions (
                message_id      INTEGER PRIMARY KEY,
                user_id         INTEGER NOT NULL,
                confession_text TEXT NOT NULL,
                posted_date     TEXT NOT NULL,
                reaction_count  INTEGER NOT NULL DEFAULT 0,
                comment_count   INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


def track_confession(message_id: int, user_id: int, text: str, today=None) -> None:
    today_str = (today or date.today()).isoformat()
    with closing(_connect()) as conn:
        conn.execute(
            """
            INSERT INTO tracked_confessions (message_id, user_id, confession_text, posted_date)
            VALUES (?, ?, ?, ?)
            """,
            (message_id, user_id, text, today_str),
        )
        conn.commit()


def update_reaction_count(message_id: int, new_total: int) -> None:
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE tracked_confessions SET reaction_count = ? WHERE message_id = ?",
            (new_total, message_id),
        )
        conn.commit()


def increment_comment_count(channel_message_id: int) -> None:
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE tracked_confessions SET comment_count = comment_count + 1 WHERE message_id = ?",
            (channel_message_id,),
        )
        conn.commit()


def is_tracked(message_id: int) -> bool:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT 1 FROM tracked_confessions WHERE message_id = ?", (message_id,)
        )
        return cur.fetchone() is not None


def get_todays_winner(today=None):
    today_str = (today or date.today()).isoformat()
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            SELECT *, (reaction_count + comment_count) AS score
            FROM tracked_confessions
            WHERE posted_date = ?
            ORDER BY score DESC, message_id ASC
            LIMIT 1
            """,
            (today_str,),
        )
        return cur.fetchone()
