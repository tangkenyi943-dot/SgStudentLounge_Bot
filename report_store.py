"""
Confession reports.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from tz_utils import now_sgt

DB_PATH = Path(__file__).parent / "confessions.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_reports_db() -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                report_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id       TEXT NOT NULL,
                reporter_id   INTEGER NOT NULL,
                reported_at   TEXT NOT NULL,
                reviewed      INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


def add_report(post_id: str, reporter_id: int) -> int:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "INSERT INTO reports (post_id, reporter_id, reported_at) VALUES (?, ?, ?)",
            (post_id.strip().upper(), reporter_id, now_sgt().isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def get_unreviewed_reports(limit: int = 20):
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT * FROM reports WHERE reviewed = 0 ORDER BY report_id ASC LIMIT ?",
            (limit,),
        )
        return cur.fetchall()


def mark_reviewed(report_id: int) -> None:
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE reports SET reviewed = 1 WHERE report_id = ?", (report_id,)
        )
        conn.commit()
