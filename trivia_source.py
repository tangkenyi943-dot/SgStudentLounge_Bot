"""
Trivia question source — Open Trivia DB API integration.

Uses opentdb.com's free, no-key-required API. Session tokens ensure the
API never serves the same question twice to us for the life of that
session (the API tracks this server-side); we additionally cache fetched
questions locally so multiple users requesting trivia in close succession
don't all trigger separate API calls.

API shape (per opentdb.com/api_config.php):
  GET https://opentdb.com/api.php?amount=N&difficulty=easy|medium|hard&type=multiple&token=...
  Returns: {"response_code": 0, "results": [{"category", "type", "difficulty",
            "question", "correct_answer", "incorrect_answers": [...]}]}

Answers and questions come HTML-entity-encoded (e.g. &quot;) by default;
we decode them before use.
"""

import html
import random
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import requests

from config import DB_DIR

DB_PATH = Path(DB_DIR) / "confessions.db"

OPENTDB_BASE = "https://opentdb.com/api.php"
OPENTDB_TOKEN_URL = "https://opentdb.com/api_token.php"

DIFFICULTIES = ["easy", "medium", "hard"]
DIFFICULTY_LABELS = {"easy": "🟢 Easy", "medium": "🟡 Medium", "hard": "🔴 Hard"}

# How many questions to keep buffered locally per difficulty, refilled
# from the API as they're consumed. Keeps us from hitting the API on
# every single question request.
BUFFER_TARGET = 20

_session_token = None
_session_token_fetched_at = 0
TOKEN_LIFETIME_SECONDS = 6 * 60 * 60  # opentdb tokens expire after 6h of inactivity


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_trivia_cache_db() -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trivia_question_cache (
                cache_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                difficulty      TEXT NOT NULL,
                question        TEXT NOT NULL,
                correct_answer  TEXT NOT NULL,
                incorrect_answers TEXT NOT NULL,
                category        TEXT,
                used            INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


def _get_session_token() -> str | None:
    """Returns a cached session token, fetching a fresh one if needed/expired."""
    global _session_token, _session_token_fetched_at
    now = time.time()
    if _session_token and (now - _session_token_fetched_at) < TOKEN_LIFETIME_SECONDS:
        return _session_token

    try:
        resp = requests.get(OPENTDB_TOKEN_URL, params={"command": "request"}, timeout=10)
        data = resp.json()
        if data.get("response_code") == 0:
            _session_token = data["token"]
            _session_token_fetched_at = now
            return _session_token
    except Exception:
        pass
    return None  # fall back to no-token requests if this fails; just means possible repeats


def _fetch_from_api(difficulty: str, amount: int = 10) -> list[dict]:
    """Fetches fresh questions from the live API. Returns [] on any failure."""
    params = {
        "amount": amount,
        "difficulty": difficulty,
        "type": "multiple",
    }
    token = _get_session_token()
    if token:
        params["token"] = token

    try:
        resp = requests.get(OPENTDB_BASE, params=params, timeout=10)
        data = resp.json()
    except Exception:
        return []

    if data.get("response_code") != 0:
        return []

    results = []
    for item in data.get("results", []):
        results.append({
            "question": html.unescape(item["question"]),
            "correct_answer": html.unescape(item["correct_answer"]),
            "incorrect_answers": [html.unescape(a) for a in item["incorrect_answers"]],
            "category": html.unescape(item.get("category", "")),
            "difficulty": difficulty,
        })
    return results


def _refill_buffer(difficulty: str) -> int:
    """Fetches more questions from the API and adds them to the local cache. Returns how many were added."""
    fetched = _fetch_from_api(difficulty, amount=10)
    if not fetched:
        return 0

    with closing(_connect()) as conn:
        for q in fetched:
            conn.execute(
                """
                INSERT INTO trivia_question_cache
                    (difficulty, question, correct_answer, incorrect_answers, category, used)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (difficulty, q["question"], q["correct_answer"], "|||".join(q["incorrect_answers"]), q["category"]),
            )
        conn.commit()
    return len(fetched)


def get_question(difficulty: str) -> dict | None:
    """
    Returns one unused question of the given difficulty, marking it used.
    Transparently refills the local cache from the API if it's running low.
    Returns None only if the API is completely unreachable AND the cache
    is empty (should be rare in practice).
    """
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"Unknown difficulty: {difficulty}")

    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS c FROM trivia_question_cache WHERE difficulty = ? AND used = 0",
            (difficulty,),
        )
        remaining = cur.fetchone()["c"]

    if remaining < 5:
        _refill_buffer(difficulty)

    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM trivia_question_cache WHERE difficulty = ? AND used = 0 ORDER BY RANDOM() LIMIT 1",
            (difficulty,),
        ).fetchone()

        if row is None:
            return None

        conn.execute(
            "UPDATE trivia_question_cache SET used = 1 WHERE cache_id = ?", (row["cache_id"],)
        )
        conn.commit()

    incorrect = row["incorrect_answers"].split("|||")
    options = incorrect + [row["correct_answer"]]
    random.shuffle(options)

    return {
        "question": row["question"],
        "correct_answer": row["correct_answer"],
        "options": options,
        "category": row["category"],
        "difficulty": difficulty,
    }


def get_random_difficulty() -> str:
    """Used for daily trivia, which doesn't need a specific difficulty input."""
    return random.choice(DIFFICULTIES)
