"""
Daily Trivia game.

Despite the name (kept for continuity with existing commands/menus),
this is no longer a single once-a-day question. It's now unlimited play
throughout the day, rate-limited to 5 questions every 30 minutes — the
same pacing as 1v1 Trivia (see trivia_1v1.py) — with the same
difficulty-weighted scoring (Easy/Medium/Hard), since unlimited replay
makes a flat per-answer point value unbalanced against other games.
"""

import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from config import DB_DIR
from trivia_source import DIFFICULTIES, get_question, get_random_difficulty

DB_PATH = Path(DB_DIR) / "confessions.db"

GAME_NAME = "daily_trivia"

DIFFICULTY_POINTS = {"easy": 2, "medium": 4, "hard": 6}

MAX_QUESTIONS_PER_30MIN = 5


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_daily_trivia_db() -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_trivia_answers (
                answer_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                difficulty      TEXT NOT NULL,
                question        TEXT NOT NULL,
                correct_answer  TEXT NOT NULL,
                options         TEXT NOT NULL,
                answered        INTEGER NOT NULL DEFAULT 0,
                was_correct     INTEGER,
                answered_at     TEXT
            )
            """
        )
        conn.commit()


def _questions_answered_in_last_30min(user_id: int, now) -> int:
    cutoff = (now - timedelta(minutes=30)).isoformat()
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            SELECT COUNT(*) AS c FROM daily_trivia_answers
            WHERE user_id = ? AND answered_at >= ?
            """,
            (user_id, cutoff),
        )
        return cur.fetchone()["c"]


def get_next_question(user_id: int, now) -> dict:
    """
    Returns the next question for this player, respecting the
    5-per-30-minutes rate limit. Returns:
    {"ok": bool, "error": str, "question": str, "options": [...], "difficulty": str}
    If they already have an unanswered question pending, returns that
    same one again rather than generating a new one (so re-checking
    doesn't burn through the rate limit or orphan a question).
    """
    with closing(_connect()) as conn:
        pending = conn.execute(
            "SELECT * FROM daily_trivia_answers WHERE user_id = ? AND answered = 0 ORDER BY answer_id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if pending is not None:
            options = pending["options"].split("|||")
            return {
                "ok": True, "error": "", "question": pending["question"],
                "options": options, "difficulty": pending["difficulty"],
            }

    if _questions_answered_in_last_30min(user_id, now) >= MAX_QUESTIONS_PER_30MIN:
        return {"ok": False, "error": f"You've hit the limit of {MAX_QUESTIONS_PER_30MIN} questions this 30 minutes. Try again later."}

    difficulty = get_random_difficulty()
    q = get_question(difficulty)
    if q is None:
        return {"ok": False, "error": "Couldn't fetch a trivia question right now — please try again in a moment."}

    options_str = "|||".join(q["options"])
    with closing(_connect()) as conn:
        conn.execute(
            """
            INSERT INTO daily_trivia_answers
                (user_id, difficulty, question, correct_answer, options)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, difficulty, q["question"], q["correct_answer"], options_str),
        )
        conn.commit()

    return {"ok": True, "error": "", "question": q["question"], "options": q["options"], "difficulty": difficulty}


def submit_answer(user_id: int, chosen_answer: str, now) -> dict:
    """
    Records the answer to whichever question is currently pending for
    this user. Returns:
    {"ok": bool, "error": str, "correct": bool, "correct_answer": str,
     "points_awarded": int, "difficulty": str}
    """
    with closing(_connect()) as conn:
        pending = conn.execute(
            "SELECT * FROM daily_trivia_answers WHERE user_id = ? AND answered = 0 ORDER BY answer_id DESC LIMIT 1",
            (user_id,),
        ).fetchone()

        if pending is None:
            return {"ok": False, "error": "You don't have a question waiting to be answered. Send /trivia to get one!"}

        is_correct = chosen_answer.strip() == pending["correct_answer"].strip()
        conn.execute(
            "UPDATE daily_trivia_answers SET answered = 1, was_correct = ?, answered_at = ? WHERE answer_id = ?",
            (1 if is_correct else 0, now.isoformat(), pending["answer_id"]),
        )
        conn.commit()

    points = DIFFICULTY_POINTS[pending["difficulty"]] if is_correct else 0
    return {
        "ok": True, "error": "", "correct": is_correct,
        "correct_answer": pending["correct_answer"],
        "points_awarded": points, "difficulty": pending["difficulty"],
    }
