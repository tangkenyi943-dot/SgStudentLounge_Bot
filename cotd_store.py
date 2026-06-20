"""
Confession of the Day, plus Post ID + category tracking for confessions.
"""

import secrets
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from config import DB_DIR

from tz_utils import now_sgt, today_sgt

DB_PATH = Path(DB_DIR) / "confessions.db"

CATEGORIES = {
    "rant": "😤 Rant",
    "love": "💕 Love",
    "help": "🆘 Help",
    "study": "📚 Study",
    "debate": "⚖️ Debate",
}


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
                comment_count   INTEGER NOT NULL DEFAULT 0,
                post_id         TEXT UNIQUE,
                category        TEXT,
                posted_at       TEXT,
                points_settled  INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


def _generate_post_id() -> str:
    """8-character uppercase alphanumeric Post ID, e.g. CYSHDZ4Y."""
    return secrets.token_hex(4).upper()


def _unique_post_id(conn) -> str:
    for _ in range(10):
        candidate = _generate_post_id()
        cur = conn.execute(
            "SELECT 1 FROM tracked_confessions WHERE post_id = ?", (candidate,)
        )
        if cur.fetchone() is None:
            return candidate
    raise RuntimeError("Could not generate a unique Post ID after 10 attempts")


def get_by_post_id(post_id: str) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT * FROM tracked_confessions WHERE post_id = ?", (post_id.strip().upper(),)
        )
        return cur.fetchone()


def get_my_confessions(user_id: int, limit: int = 10) -> list[sqlite3.Row]:
    """
    Returns this user's own posted confessions, most-reacted-to first.
    Used for letting someone browse which of their own posts landed well.
    """
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            SELECT *, (reaction_count + comment_count) AS score
            FROM tracked_confessions
            WHERE user_id = ?
            ORDER BY score DESC, posted_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        return cur.fetchall()


def get_random_confession() -> sqlite3.Row | None:
    """Returns one genuinely random confession from all-time, or None if there are none yet."""
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT * FROM tracked_confessions ORDER BY RANDOM() LIMIT 1"
        )
        return cur.fetchone()


def track_confession(
    message_id: int, user_id: int, text: str, category: str | None = None, today: date | None = None
) -> str:
    """Call this right after posting a confession to the channel. Returns the generated Post ID."""
    now = now_sgt()
    today_str = (today or now.date()).isoformat()
    with closing(_connect()) as conn:
        post_id = _unique_post_id(conn)
        conn.execute(
            """
            INSERT INTO tracked_confessions
                (message_id, user_id, confession_text, posted_date, post_id, category, posted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, user_id, text, today_str, post_id, category, now.isoformat()),
        )
        conn.commit()
    return post_id


CONFESSION_RATE_LIMIT_COUNT = 2
CONFESSION_RATE_LIMIT_MINUTES = 30


def check_confession_rate_limit(user_id: int) -> tuple[bool, int]:
    """
    Returns (is_allowed, minutes_until_next_slot). is_allowed is False if
    the user has already posted CONFESSION_RATE_LIMIT_COUNT confessions
    within the last CONFESSION_RATE_LIMIT_MINUTES minutes.
    """
    cutoff = (now_sgt() - timedelta(minutes=CONFESSION_RATE_LIMIT_MINUTES)).isoformat()
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            SELECT posted_at FROM tracked_confessions
            WHERE user_id = ? AND posted_at >= ?
            ORDER BY posted_at ASC
            """,
            (user_id, cutoff),
        )
        recent = cur.fetchall()

    if len(recent) < CONFESSION_RATE_LIMIT_COUNT:
        return True, 0

    oldest_relevant = datetime.fromisoformat(recent[0]["posted_at"])
    unlock_time = oldest_relevant + timedelta(minutes=CONFESSION_RATE_LIMIT_MINUTES)
    minutes_left = max(1, int((unlock_time - now_sgt()).total_seconds() // 60) + 1)
    return False, minutes_left


def update_reaction_count(message_id: int, new_total: int) -> None:
    """Call this whenever a message_reaction_count update fires for a tracked message."""
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE tracked_confessions SET reaction_count = ? WHERE message_id = ?",
            (new_total, message_id),
        )
        conn.commit()


def increment_comment_count(channel_message_id: int) -> None:
    """Call this whenever a new comment arrives in the discussion group for a tracked post."""
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


def get_todays_winner(today: date | None = None) -> sqlite3.Row | None:
    """
    Returns the tracked confession from today with the highest combined
    score (reactions + comments, valued equally), or None if there were
    no confessions today.
    """
    today_str = (today or today_sgt()).isoformat()
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


REACTION_POINTS_MULTIPLIER = 2
REACTION_POINTS_DELAY_MINUTES = 60


def get_confessions_ready_to_settle() -> list[sqlite3.Row]:
    """
    Returns confessions posted >= REACTION_POINTS_DELAY_MINUTES ago that
    haven't had their reaction-based points awarded yet. Call this from a
    periodic job; award points based on whatever reaction_count has
    accumulated by the time this runs, then call mark_points_settled() for
    each one so they're never paid out twice.
    """
    cutoff = (now_sgt() - timedelta(minutes=REACTION_POINTS_DELAY_MINUTES)).isoformat()
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            SELECT * FROM tracked_confessions
            WHERE points_settled = 0 AND posted_at <= ?
            """,
            (cutoff,),
        )
        return cur.fetchall()


def mark_points_settled(message_id: int) -> None:
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE tracked_confessions SET points_settled = 1 WHERE message_id = ?",
            (message_id,),
        )
        conn.commit()
