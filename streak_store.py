"""
Activity streak tracking.

A streak counts consecutive CALENDAR DAYS on which the user did ANY
qualifying activity: played Wordle (win or loss), played Fishing (cast +
reel), or posted a confession. Any one of these on a given day keeps the
streak alive — they're not tracked separately. Call record_play() once
per user per day, right after any of those three actions completes; it's
safe to call multiple times in the same day (subsequent calls are a no-op
for that day, see below).

Milestones award a one-time bonus the first time a streak reaches that
length. Hitting day 10 after already having passed day 5 doesn't re-award
day 5 — each milestone fires exactly once per account, ever, tracked via
last_milestone_awarded.
"""

import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from config import DB_DIR

from tz_utils import today_sgt

DB_PATH = Path(DB_DIR) / "confessions.db"

MILESTONE_BONUSES = {
    5: 20,
    10: 50,
    20: 100,
    50: 250,
}


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_streak_db() -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wordle_streaks (
                user_id             INTEGER PRIMARY KEY,
                current_streak      INTEGER NOT NULL DEFAULT 0,
                longest_streak      INTEGER NOT NULL DEFAULT 0,
                last_played_date    TEXT,
                last_milestone_hit  INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


def get_streak(user_id: int) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT * FROM wordle_streaks WHERE user_id = ?", (user_id,)
        )
        return cur.fetchone()


def record_play(user_id: int, today: date | None = None) -> dict:
    """
    Call this once per user, once per day, right after their Wordle game
    finishes (win or loss). Updates the streak and returns:
    {"current_streak": int, "milestone_hit": int or None, "bonus_points": int}

    Safe to call more than once for the same day for the same user — it's
    a no-op if last_played_date is already today (prevents double-counting
    if called from multiple places by mistake).
    """
    today = today or today_sgt()
    today_str = today.isoformat()

    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM wordle_streaks WHERE user_id = ?", (user_id,)
        ).fetchone()

        if row is None:
            new_streak = 1
            longest = 1
            last_milestone = 0
        elif row["last_played_date"] == today_str:
            # Already recorded today — no-op, return current state.
            return {
                "current_streak": row["current_streak"],
                "milestone_hit": None,
                "bonus_points": 0,
            }
        else:
            last_played = date.fromisoformat(row["last_played_date"])
            if last_played == today - timedelta(days=1):
                new_streak = row["current_streak"] + 1
            else:
                new_streak = 1  # streak broken, restart
            longest = max(row["longest_streak"], new_streak)
            last_milestone = row["last_milestone_hit"]

        milestone_hit = None
        bonus_points = 0
        for milestone, bonus in sorted(MILESTONE_BONUSES.items()):
            if new_streak >= milestone and last_milestone < milestone:
                milestone_hit = milestone
                bonus_points = bonus
                last_milestone = milestone
                # Only the highest newly-crossed milestone in one jump
                # counts (shouldn't normally skip multiple in one day, but
                # defensive in case streak data is edited manually).

        conn.execute(
            """
            INSERT INTO wordle_streaks
                (user_id, current_streak, longest_streak, last_played_date, last_milestone_hit)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                current_streak = excluded.current_streak,
                longest_streak = excluded.longest_streak,
                last_played_date = excluded.last_played_date,
                last_milestone_hit = excluded.last_milestone_hit
            """,
            (user_id, new_streak, longest, today_str, last_milestone),
        )
        conn.commit()

    return {
        "current_streak": new_streak,
        "milestone_hit": milestone_hit,
        "bonus_points": bonus_points,
    }


def get_users_at_risk(today: date | None = None) -> list[sqlite3.Row]:
    """
    Returns everyone with an active streak (current_streak > 0) who has
    NOT played today and whose last play was yesterday (i.e. they'd lose
    their streak if they don't play before midnight). Used for the evening
    reminder DM.
    """
    today = today or today_sgt()
    yesterday_str = (today - timedelta(days=1)).isoformat()

    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            SELECT * FROM wordle_streaks
            WHERE current_streak > 0 AND last_played_date = ?
            """,
            (yesterday_str,),
        )
        return cur.fetchall()
