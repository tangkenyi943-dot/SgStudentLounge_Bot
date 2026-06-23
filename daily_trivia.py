"""
Daily Trivia game.

One question per day, random difficulty, sourced live from trivia_source
(Open Trivia DB). Correct answer awards a flat 30 points regardless of
difficulty (daily trivia doesn't use the difficulty-weighted scoring that
1v1 Trivia does — see trivia_1v1.py — since it's a single one-shot
question per day, not a volume-based competition).
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from config import DB_DIR
from trivia_source import get_question, get_random_difficulty

DB_PATH = Path(DB_DIR) / "confessions.db"

GAME_NAME = "daily_trivia"
DAILY_TRIVIA_POINTS = 30


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_daily_trivia_db() -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_trivia_state (
                user_id         INTEGER NOT NULL,
                trivia_date     TEXT NOT NULL,
                question        TEXT NOT NULL,
                correct_answer  TEXT NOT NULL,
                options         TEXT NOT NULL,
                difficulty      TEXT NOT NULL,
                answered        INTEGER NOT NULL DEFAULT 0,
                was_correct     INTEGER,
                PRIMARY KEY (user_id, trivia_date)
            )
            """
        )
        conn.commit()


def get_or_create_todays_question(user_id: int, today_str: str) -> sqlite3.Row:
    """
    Returns this user's question for today, generating a fresh one (and
    persisting it) if they haven't started today's trivia yet. Persisting
    per-user means everyone gets a genuinely different question each day
    (pulled fresh from the API), rather than one shared daily question —
    a deliberate difference from Wordle's shared-word design, chosen so
    daily trivia doesn't get "spoiled" by someone posting the answer.
    """
    with closing(_connect()) as conn:
        existing = conn.execute(
            "SELECT * FROM daily_trivia_state WHERE user_id = ? AND trivia_date = ?",
            (user_id, today_str),
        ).fetchone()
        if existing is not None:
            return existing

        difficulty = get_random_difficulty()
        q = get_question(difficulty)
        if q is None:
            raise RuntimeError("Trivia question source unavailable")

        options_str = "|||".join(q["options"])
        conn.execute(
            """
            INSERT INTO daily_trivia_state
                (user_id, trivia_date, question, correct_answer, options, difficulty)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, today_str, q["question"], q["correct_answer"], options_str, difficulty),
        )
        conn.commit()

        return conn.execute(
            "SELECT * FROM daily_trivia_state WHERE user_id = ? AND trivia_date = ?",
            (user_id, today_str),
        ).fetchone()


def get_todays_state(user_id: int, today_str: str) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT * FROM daily_trivia_state WHERE user_id = ? AND trivia_date = ?",
            (user_id, today_str),
        ).fetchone()


def submit_answer(user_id: int, today_str: str, chosen_answer: str) -> dict:
    """
    Records the user's answer for today. Returns:
    {"ok": bool, "error": str, "correct": bool, "correct_answer": str, "points_awarded": int}
    """
    state = get_todays_state(user_id, today_str)
    if state is None:
        return {"ok": False, "error": "You haven't started today's trivia yet."}
    if state["answered"]:
        return {"ok": False, "error": "You've already answered today's trivia. Come back tomorrow!"}

    is_correct = chosen_answer.strip() == state["correct_answer"].strip()

    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE daily_trivia_state SET answered = 1, was_correct = ? WHERE user_id = ? AND trivia_date = ?",
            (1 if is_correct else 0, user_id, today_str),
        )
        conn.commit()

    return {
        "ok": True,
        "error": "",
        "correct": is_correct,
        "correct_answer": state["correct_answer"],
        "points_awarded": DAILY_TRIVIA_POINTS if is_correct else 0,
    }
