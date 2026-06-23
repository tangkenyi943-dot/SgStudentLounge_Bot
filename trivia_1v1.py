"""
1v1 Trivia.

Two players (challenger + opponent, via direct challenge or random
matchmaking) compete over a 24-hour round. Each can answer up to 5
questions per hour, no fixed total limit otherwise. Each person gets
their OWN distinct question content, but the DIFFICULTY SEQUENCE is
synced between them — e.g. both players' 3rd question is the same
difficulty tier, even though the actual questions differ. This keeps the
matchup fair (neither player can get luckier with an easier mix) while
still preventing one player from seeing the other's exact answer if they
were somehow shown the same question.

Scoring is difficulty-weighted (see DIFFICULTY_POINTS) and is based on
TOTAL weighted points, not raw correct-count or accuracy — so someone who
answers more questions has more opportunities to score, same as Wordle
rewards trying.
"""

import random
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from config import DB_DIR
from tz_utils import now_sgt
from trivia_source import DIFFICULTIES, get_question

DB_PATH = Path(DB_DIR) / "confessions.db"

GAME_NAME = "trivia_1v1"

DIFFICULTY_POINTS = {"easy": 2, "medium": 4, "hard": 6}

ROUND_DURATION_HOURS = 24
MAX_QUESTIONS_PER_HOUR = 5


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_trivia_1v1_db() -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trivia_1v1_matches (
                match_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                player_a_id     INTEGER NOT NULL,
                player_b_id     INTEGER,
                started_at      TEXT NOT NULL,
                ends_at         TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                difficulty_sequence TEXT NOT NULL DEFAULT '',
                winner_id       INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trivia_1v1_answers (
                answer_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id        INTEGER NOT NULL,
                user_id         INTEGER NOT NULL,
                question_index  INTEGER NOT NULL,
                difficulty      TEXT NOT NULL,
                question        TEXT NOT NULL,
                correct_answer  TEXT NOT NULL,
                options         TEXT NOT NULL,
                answered        INTEGER NOT NULL DEFAULT 0,
                was_correct     INTEGER,
                answered_at     TEXT,
                UNIQUE(match_id, user_id, question_index)
            )
            """
        )
        conn.commit()


def create_challenge(player_a_id: int, player_b_id: int | None, now=None) -> int:
    """
    Starts a new match. player_b_id is None for an open random-matchmaking
    challenge (waiting for someone to accept); set for a direct challenge.
    Returns the new match_id.
    """
    now = now or now_sgt()
    ends_at = now + timedelta(hours=ROUND_DURATION_HOURS)
    status = "active" if player_b_id else "pending"

    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            INSERT INTO trivia_1v1_matches
                (player_a_id, player_b_id, started_at, ends_at, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (player_a_id, player_b_id, now.isoformat(), ends_at.isoformat(), status),
        )
        conn.commit()
        return cur.lastrowid


def accept_challenge(match_id: int, player_b_id: int, now=None) -> bool:
    """Accepts an open match. Returns False if it's no longer available."""
    now = now or now_sgt()
    with closing(_connect()) as conn:
        match = conn.execute(
            "SELECT * FROM trivia_1v1_matches WHERE match_id = ?", (match_id,)
        ).fetchone()
        if match is None or match["status"] != "pending":
            return False

        ends_at = now + timedelta(hours=ROUND_DURATION_HOURS)
        conn.execute(
            "UPDATE trivia_1v1_matches SET player_b_id = ?, status = 'active', started_at = ?, ends_at = ? WHERE match_id = ?",
            (player_b_id, now.isoformat(), ends_at.isoformat(), match_id),
        )
        conn.commit()
        return True


def get_open_challenges(exclude_user_id: int, limit: int = 5) -> list[sqlite3.Row]:
    """Returns pending (unaccepted) matches someone could join, excluding their own."""
    with closing(_connect()) as conn:
        return conn.execute(
            """
            SELECT * FROM trivia_1v1_matches
            WHERE status = 'pending' AND player_a_id != ?
            ORDER BY started_at ASC
            LIMIT ?
            """,
            (exclude_user_id, limit),
        ).fetchall()


def get_active_match_for_user(user_id: int, now=None) -> sqlite3.Row | None:
    now = now or now_sgt()
    with closing(_connect()) as conn:
        return conn.execute(
            """
            SELECT * FROM trivia_1v1_matches
            WHERE status = 'active' AND (player_a_id = ? OR player_b_id = ?)
              AND ends_at > ?
            ORDER BY started_at DESC LIMIT 1
            """,
            (user_id, user_id, now.isoformat()),
        ).fetchone()


def _questions_answered_in_last_hour(match_id: int, user_id: int, now) -> int:
    cutoff = (now - timedelta(hours=1)).isoformat()
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            SELECT COUNT(*) AS c FROM trivia_1v1_answers
            WHERE match_id = ? AND user_id = ? AND answered_at >= ?
            """,
            (match_id, user_id, cutoff),
        )
        return cur.fetchone()["c"]


def _next_question_index(match_id: int, user_id: int) -> int:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT COALESCE(MAX(question_index), -1) AS m FROM trivia_1v1_answers WHERE match_id = ? AND user_id = ?",
            (match_id, user_id),
        )
        return cur.fetchone()["m"] + 1


def _get_or_assign_difficulty(match_id: int, question_index: int) -> str:
    """
    Ensures both players share the same difficulty at a given question
    index. The FIRST player to reach a given index picks (randomly) the
    difficulty for that index; it's stored on the match and reused for
    the other player when they reach the same index.
    """
    with closing(_connect()) as conn:
        match = conn.execute(
            "SELECT difficulty_sequence FROM trivia_1v1_matches WHERE match_id = ?", (match_id,)
        ).fetchone()
        sequence = match["difficulty_sequence"].split(",") if match["difficulty_sequence"] else []

        if question_index < len(sequence):
            return sequence[question_index]

        # Extend the sequence up to this index with fresh random difficulties.
        while len(sequence) <= question_index:
            sequence.append(random.choice(DIFFICULTIES))

        conn.execute(
            "UPDATE trivia_1v1_matches SET difficulty_sequence = ? WHERE match_id = ?",
            (",".join(sequence), match_id),
        )
        conn.commit()
        return sequence[question_index]


def start_next_question(match_id: int, user_id: int, now=None) -> dict:
    """
    Returns the next question for this player in this match, respecting
    the hourly rate limit and the difficulty-sync rule. Returns:
    {"ok": bool, "error": str, "question": str, "options": [...],
     "difficulty": str, "question_index": int}
    """
    now = now or now_sgt()

    with closing(_connect()) as conn:
        match = conn.execute(
            "SELECT * FROM trivia_1v1_matches WHERE match_id = ?", (match_id,)
        ).fetchone()

    if match is None or match["status"] != "active":
        return {"ok": False, "error": "This match isn't active."}

    if now.isoformat() > match["ends_at"]:
        return {"ok": False, "error": "This match has ended."}

    if _questions_answered_in_last_hour(match_id, user_id, now) >= MAX_QUESTIONS_PER_HOUR:
        return {"ok": False, "error": f"You've hit the limit of {MAX_QUESTIONS_PER_HOUR} questions this hour. Try again later."}

    # Don't hand out a new question if they already have an unanswered one pending.
    with closing(_connect()) as conn:
        pending = conn.execute(
            "SELECT * FROM trivia_1v1_answers WHERE match_id = ? AND user_id = ? AND answered = 0",
            (match_id, user_id),
        ).fetchone()
        if pending is not None:
            options = pending["options"].split("|||")
            return {
                "ok": True, "error": "", "question": pending["question"], "options": options,
                "difficulty": pending["difficulty"], "question_index": pending["question_index"],
            }

    question_index = _next_question_index(match_id, user_id)
    difficulty = _get_or_assign_difficulty(match_id, question_index)
    q = get_question(difficulty)
    if q is None:
        return {"ok": False, "error": "Couldn't fetch a question right now, try again shortly."}

    options_str = "|||".join(q["options"])
    with closing(_connect()) as conn:
        conn.execute(
            """
            INSERT INTO trivia_1v1_answers
                (match_id, user_id, question_index, difficulty, question, correct_answer, options)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (match_id, user_id, question_index, difficulty, q["question"], q["correct_answer"], options_str),
        )
        conn.commit()

    return {
        "ok": True, "error": "", "question": q["question"], "options": q["options"],
        "difficulty": difficulty, "question_index": question_index,
    }


def submit_1v1_answer(match_id: int, user_id: int, chosen_answer: str, now=None) -> dict:
    now = now or now_sgt()
    with closing(_connect()) as conn:
        pending = conn.execute(
            "SELECT * FROM trivia_1v1_answers WHERE match_id = ? AND user_id = ? AND answered = 0 ORDER BY question_index DESC LIMIT 1",
            (match_id, user_id),
        ).fetchone()

        if pending is None:
            return {"ok": False, "error": "You don't have a question waiting to be answered."}

        is_correct = chosen_answer.strip() == pending["correct_answer"].strip()
        conn.execute(
            "UPDATE trivia_1v1_answers SET answered = 1, was_correct = ?, answered_at = ? WHERE answer_id = ?",
            (1 if is_correct else 0, now.isoformat(), pending["answer_id"]),
        )
        conn.commit()

    points = DIFFICULTY_POINTS[pending["difficulty"]] if is_correct else 0
    return {
        "ok": True, "error": "", "correct": is_correct,
        "correct_answer": pending["correct_answer"], "points_awarded": points,
        "difficulty": pending["difficulty"],
    }


def get_match_score(match_id: int, user_id: int) -> int:
    """Total weighted score for one player in one match, from correct answers so far."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT difficulty FROM trivia_1v1_answers WHERE match_id = ? AND user_id = ? AND was_correct = 1",
            (match_id, user_id),
        ).fetchall()
    return sum(DIFFICULTY_POINTS[r["difficulty"]] for r in rows)


def finalize_expired_matches(now=None) -> list[sqlite3.Row]:
    """
    Finds active matches whose 24h window has passed, marks them
    completed with a winner, and returns the list of finalized matches
    (so the caller can announce results / award points).
    """
    now = now or now_sgt()
    with closing(_connect()) as conn:
        expired = conn.execute(
            "SELECT * FROM trivia_1v1_matches WHERE status = 'active' AND ends_at <= ?",
            (now.isoformat(),),
        ).fetchall()

        finalized = []
        for match in expired:
            score_a = get_match_score(match["match_id"], match["player_a_id"])
            score_b = get_match_score(match["match_id"], match["player_b_id"]) if match["player_b_id"] else 0

            if score_a > score_b:
                winner_id = match["player_a_id"]
            elif score_b > score_a:
                winner_id = match["player_b_id"]
            else:
                winner_id = None  # tie

            conn.execute(
                "UPDATE trivia_1v1_matches SET status = 'completed', winner_id = ? WHERE match_id = ?",
                (winner_id, match["match_id"]),
            )
            finalized.append({
                "match_id": match["match_id"],
                "player_a_id": match["player_a_id"],
                "player_b_id": match["player_b_id"],
                "score_a": score_a,
                "score_b": score_b,
                "winner_id": winner_id,
            })
        conn.commit()
        return finalized
