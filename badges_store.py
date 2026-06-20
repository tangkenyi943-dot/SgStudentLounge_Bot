"""
Achievement badges.

Each badge is checked and awarded at the moment a relevant action happens
(e.g. right after a confession posts, right after a Wordle win, right
after a fish is caught) — there's no periodic scan. This means badges are
awarded going forward from when this feature was introduced; past
qualifying actions won't retroactively earn a badge.

Badges are awarded at most once per user — re-triggering the same
condition (e.g. posting an 11th confession after already having
"Storyteller") is a silent no-op.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from config import DB_DIR
from tz_utils import now_sgt

DB_PATH = Path(DB_DIR) / "confessions.db"

BADGES = {
    "first_steps": ("🐣", "First Steps", "Posted your first confession"),
    "storyteller": ("📢", "Storyteller", "Posted 10 confessions"),
    "viral": ("🌟", "Viral", "A confession hit 20+ reactions"),
    "word_wizard": ("🟩", "Word Wizard", "Won Wordle in 1 guess"),
    "dedicated": ("🔥", "Dedicated", "Hit a 5-day Wordle streak"),
    "centurion": ("🏅", "Centurion", "Hit a 50-day Wordle streak"),
    "first_catch": ("🎣", "First Catch", "Caught your first fish"),
    "jackpot": ("💎", "Jackpot", "Caught a Secret-tier fish"),
    "collector": ("🐠", "Collector", "Caught every unique fish species"),
}


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_badges_db() -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS badges (
                user_id     INTEGER NOT NULL,
                badge_key   TEXT NOT NULL,
                earned_at   TEXT NOT NULL,
                PRIMARY KEY (user_id, badge_key)
            )
            """
        )
        conn.commit()


def has_badge(user_id: int, badge_key: str) -> bool:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT 1 FROM badges WHERE user_id = ? AND badge_key = ?",
            (user_id, badge_key),
        )
        return cur.fetchone() is not None


def award_badge(user_id: int, badge_key: str) -> bool:
    """
    Awards a badge if not already earned. Returns True if this call
    actually newly awarded it (so the caller can announce it), False if
    they already had it (silent no-op).
    """
    if badge_key not in BADGES:
        raise ValueError(f"Unknown badge key: {badge_key}")

    with closing(_connect()) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO badges (user_id, badge_key, earned_at) VALUES (?, ?, ?)",
            (user_id, badge_key, now_sgt().isoformat()),
        )
        conn.commit()
        return cur.rowcount > 0


def get_user_badges(user_id: int) -> list[sqlite3.Row]:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT * FROM badges WHERE user_id = ? ORDER BY earned_at ASC",
            (user_id,),
        )
        return cur.fetchall()
