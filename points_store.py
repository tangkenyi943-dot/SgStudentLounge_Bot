"""
Points and leaderboard system.

Designed to be game-agnostic: any game module just calls award_points()
with a game name, and this module handles all storage, totals, and
leaderboard queries. Games don't need to know anything about SQL or how
points are stored — they just report outcomes.

Schema:
  game_points(user_id, game, points) — one row per (user, game) pair,
  points accumulate via upsert. Global total is always computed as a
  SUM across games for a user, so there's no separate "total" column to
  keep in sync (avoids a whole class of bugs where total drifts from
  the sum of its parts).
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


def init_points_db() -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS game_points (
                user_id     INTEGER NOT NULL,
                game        TEXT NOT NULL,
                points      INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, game)
            )
            """
        )
        # Audit log of every individual point-earning event, for admin
        # visibility into how someone accumulated their total. Only tracks
        # events from when this table was introduced onward — totals
        # awarded before this existed aren't retroactively logged here.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS point_events (
                event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                game        TEXT NOT NULL,
                points      INTEGER NOT NULL,
                occurred_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def award_points(user_id: int, game: str, points: int) -> int:
    """
    Adds `points` to user's total for `game` (can be negative to deduct).
    Returns the user's new total for that game. Also logs this event for
    the admin audit trail (/pointlog).
    """
    with closing(_connect()) as conn:
        conn.execute(
            """
            INSERT INTO game_points (user_id, game, points)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, game) DO UPDATE SET points = points + excluded.points
            """,
            (user_id, game, points),
        )
        conn.execute(
            "INSERT INTO point_events (user_id, game, points, occurred_at) VALUES (?, ?, ?, ?)",
            (user_id, game, points, now_sgt().isoformat()),
        )
        conn.commit()
        cur = conn.execute(
            "SELECT points FROM game_points WHERE user_id = ? AND game = ?",
            (user_id, game),
        )
        row = cur.fetchone()
        return row["points"] if row else 0


def get_global_total(user_id: int) -> int:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT COALESCE(SUM(points), 0) AS total FROM game_points WHERE user_id = ?",
            (user_id,),
        )
        return cur.fetchone()["total"]


def get_game_points(user_id: int, game: str) -> int:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT points FROM game_points WHERE user_id = ? AND game = ?",
            (user_id, game),
        )
        row = cur.fetchone()
        return row["points"] if row else 0


def get_per_game_breakdown(user_id: int) -> list[sqlite3.Row]:
    """Returns all (game, points) rows for a user, highest first."""
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT game, points FROM game_points WHERE user_id = ? ORDER BY points DESC",
            (user_id,),
        )
        return cur.fetchall()


def get_global_leaderboard(limit: int = 20) -> list[sqlite3.Row]:
    """
    Returns top `limit` users by global total points, joined with their
    identity (username, avatar), plus their Wordle and Fishing point
    breakdowns specifically (the two columns shown alongside Total in
    /leaderboard). Other games (e.g. "confessions" reaction points) still
    count toward the total but aren't broken out as their own column.
    Users with no identity set yet are excluded since there'd be nothing
    displayable for them.
    """
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            SELECT
                i.username,
                i.avatar,
                SUM(gp.points) AS total,
                SUM(CASE WHEN gp.game = 'wordle' THEN gp.points ELSE 0 END) AS wordle_points,
                SUM(CASE WHEN gp.game = 'fishing' THEN gp.points ELSE 0 END) AS fishing_points
            FROM game_points gp
            JOIN identities i ON i.user_id = gp.user_id
            GROUP BY gp.user_id
            ORDER BY total DESC
            LIMIT ?
            """,
            (limit,),
        )
        return cur.fetchall()


def get_recent_point_events(limit: int = 30) -> list[sqlite3.Row]:
    """
    Admin tool: returns the most recent point-earning events across all
    users, newest first, joined with identity info for display. Only
    includes events logged since the audit-log feature was introduced.
    """
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            SELECT pe.*, i.username, i.avatar, i.hex_id
            FROM point_events pe
            LEFT JOIN identities i ON i.user_id = pe.user_id
            ORDER BY pe.event_id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return cur.fetchall()


def get_current_week_start():
    """
    Returns the datetime (SGT) of the most recent Monday at 00:00 — the
    start of the "current week" for the resetting weekly leaderboard.
    Weeks run Monday through Sunday, since the weekly leaderboard post
    happens Sunday evening and should reflect that just-finished week.
    """
    from datetime import datetime, time as dt_time, timedelta
    from tz_utils import SGT, now_sgt

    now = now_sgt()
    days_since_monday = now.weekday()  # Monday = 0
    monday = now.date() - timedelta(days=days_since_monday)
    return datetime.combine(monday, dt_time(0, 0), tzinfo=SGT)


def get_weekly_leaderboard(limit: int = 20) -> list[sqlite3.Row]:
    """
    Same shape as get_global_leaderboard (Total/Wordle/Fishing columns),
    but computed from point_events for ONLY this week (since the most
    recent Monday 00:00 SGT) rather than all-time totals. This is what
    naturally "resets" each week — there's no separate counter to reset,
    since the window itself just slides forward; older events fall out
    of range automatically once a new week starts.

    Note: only reflects events logged since the audit-log feature was
    introduced — points awarded before that won't appear here even if
    they technically happened this week, since they were never recorded
    with a timestamp.
    """
    week_start = get_current_week_start().isoformat()
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            SELECT
                i.username,
                i.avatar,
                SUM(pe.points) AS total,
                SUM(CASE WHEN pe.game = 'wordle' THEN pe.points ELSE 0 END) AS wordle_points,
                SUM(CASE WHEN pe.game = 'fishing' THEN pe.points ELSE 0 END) AS fishing_points
            FROM point_events pe
            JOIN identities i ON i.user_id = pe.user_id
            WHERE pe.occurred_at >= ?
            GROUP BY pe.user_id
            ORDER BY total DESC
            LIMIT ?
            """,
            (week_start, limit),
        )
        return cur.fetchall()


def get_game_leaderboard(game: str, limit: int = 20) -> list[sqlite3.Row]:
    """Same as get_global_leaderboard but scoped to a single game."""
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            SELECT i.username, i.avatar, gp.points
            FROM game_points gp
            JOIN identities i ON i.user_id = gp.user_id
            WHERE gp.game = ?
            ORDER BY gp.points DESC
            LIMIT ?
            """,
            (game, limit),
        )
        return cur.fetchall()
