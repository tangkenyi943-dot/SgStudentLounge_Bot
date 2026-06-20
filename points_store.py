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
        conn.commit()


def award_points(user_id: int, game: str, points: int) -> int:
    """
    Adds `points` to user's total for `game` (can be negative to deduct).
    Returns the user's new total for that game.
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
    identity (username, avatar) for display. Users with no identity set
    yet are excluded since there'd be nothing displayable for them.
    """
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            SELECT i.username, i.avatar, SUM(gp.points) AS total
            FROM game_points gp
            JOIN identities i ON i.user_id = gp.user_id
            GROUP BY gp.user_id
            ORDER BY total DESC
            LIMIT ?
            """,
            (limit,),
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
