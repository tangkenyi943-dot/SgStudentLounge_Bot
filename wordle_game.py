"""
Daily Wordle-style game.
"""

import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from config import DB_DIR

from tz_utils import today_sgt

DB_PATH = Path(DB_DIR) / "confessions.db"

WORD_LENGTH = 5
MAX_GUESSES = 6
GAME_NAME = "wordle"

POINTS_BY_GUESS_COUNT = {1: 50, 2: 40, 3: 30, 4: 20, 5: 10, 6: 5}

EPOCH_DATE = date(2026, 1, 1)

WORD_BANK = [
    "TRAIN", "HOTEL", "VENOM", "DREAM", "TRUTH", "CRISP", "WATER", "ZEBRA", "STORY", "QUIRK",
    "CHARM", "QUICK", "JOLLY", "KNIFE", "BLITZ", "OCEAN", "MAGIC", "FROST", "GRIME", "STORM",
    "GREEN", "BREAD", "WHITE", "QUEST", "GLYPH", "BRAVE", "CRYPT", "SLEEP", "ZESTY", "GLASS",
    "HEART", "HONEY", "NOBLE", "RADIO", "DELTA", "SHARK", "STING", "GHOST", "VAULT", "SOUND",
    "CHAIR", "PHONE", "TIGER", "MONTH", "RIVER", "URBAN", "PRIDE", "OASIS", "DANCE", "PAPER",
    "TONIC", "BERRY", "MUSIC", "FLAME", "FRUIT", "SNAKE", "MERIT", "WITTY", "MONEY", "QUILT",
    "PHOTO", "SMILE", "BEACH", "VIVID", "NYMPH", "SUGAR", "WRATH", "PLANE", "KAYAK", "PLANT",
    "HOUSE", "OPERA", "LOYAL", "GLAZE", "HUMOR", "JOUST", "PARTY", "AMBER", "PLAID", "HASTE",
    "SCARF", "VOICE", "SPACE", "PIXEL", "RUSTY", "LIGHT", "IDEAL", "FABLE", "WORLD", "YOUTH",
    "WALTZ", "TASTE", "CLOUD", "TODAY", "BLACK", "TABLE", "MUMMY", "HAPPY", "FRESH", "FJORD",
]

assert len(WORD_BANK) == 100, "Word bank must have exactly 100 words"
assert all(len(w) == WORD_LENGTH for w in WORD_BANK), "All words must be 5 letters"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_wordle_db() -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wordle_guesses (
                user_id     INTEGER NOT NULL,
                game_date   TEXT NOT NULL,
                guess_num   INTEGER NOT NULL,
                guess_word  TEXT NOT NULL,
                PRIMARY KEY (user_id, game_date, guess_num)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wordle_results (
                user_id     INTEGER NOT NULL,
                game_date   TEXT NOT NULL,
                won         INTEGER NOT NULL,
                guesses_used INTEGER NOT NULL,
                PRIMARY KEY (user_id, game_date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wordle_sessions (
                user_id     INTEGER NOT NULL,
                game_date   TEXT NOT NULL,
                PRIMARY KEY (user_id, game_date)
            )
            """
        )
        conn.commit()


def start_session(user_id: int, today=None) -> None:
    today_str = (today or today_sgt()).isoformat()
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO wordle_sessions (user_id, game_date) VALUES (?, ?)",
            (user_id, today_str),
        )
        conn.commit()


def has_active_session(user_id: int, today=None) -> bool:
    today_str = (today or today_sgt()).isoformat()
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT 1 FROM wordle_sessions WHERE user_id = ? AND game_date = ?",
            (user_id, today_str),
        )
        started = cur.fetchone() is not None
        if not started:
            return False
        cur = conn.execute(
            "SELECT 1 FROM wordle_results WHERE user_id = ? AND game_date = ?",
            (user_id, today_str),
        )
        finished = cur.fetchone() is not None
        return not finished


def _today_index(today=None) -> int:
    today = today or today_sgt()
    days_since_epoch = (today - EPOCH_DATE).days
    return days_since_epoch % len(WORD_BANK)


def get_todays_word(today=None) -> str:
    return WORD_BANK[_today_index(today)]


def get_guesses_today(user_id: int, today=None):
    today_str = (today or today_sgt()).isoformat()
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            SELECT guess_word FROM wordle_guesses
            WHERE user_id = ? AND game_date = ?
            ORDER BY guess_num ASC
            """,
            (user_id, today_str),
        )
        return [row["guess_word"] for row in cur.fetchall()]


def get_result_today(user_id: int, today=None):
    today_str = (today or today_sgt()).isoformat()
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT * FROM wordle_results WHERE user_id = ? AND game_date = ?",
            (user_id, today_str),
        )
        return cur.fetchone()


def score_guess(guess: str, answer: str) -> str:
    result = ["⬜"] * len(guess)
    answer_chars = list(answer)

    for i, ch in enumerate(guess):
        if ch == answer[i]:
            result[i] = "🟩"
            answer_chars[i] = None

    for i, ch in enumerate(guess):
        if result[i] == "🟩":
            continue
        if ch in answer_chars:
            result[i] = "🟨"
            answer_chars[answer_chars.index(ch)] = None

    return "".join(result)


def submit_guess(user_id: int, guess: str, today=None) -> dict:
    today = today or today_sgt()
    guess = guess.strip().upper()

    if len(guess) != WORD_LENGTH or not guess.isalpha():
        return {"valid": False, "error": f"Guess must be exactly {WORD_LENGTH} letters."}

    from word_validator import is_valid_word

    if not is_valid_word(guess):
        return {"valid": False, "error": f"\"{guess}\" isn't a word I recognize. Try a real 5-letter word."}

    existing_result = get_result_today(user_id, today)
    if existing_result is not None:
        return {"valid": False, "error": "You've already finished today's word. Come back tomorrow!"}

    previous_guesses = get_guesses_today(user_id, today)
    if len(previous_guesses) >= MAX_GUESSES:
        return {"valid": False, "error": "You're out of guesses for today."}

    answer = get_todays_word(today)
    guess_num = len(previous_guesses) + 1
    feedback = score_guess(guess, answer)
    won = guess == answer
    game_over = won or guess_num >= MAX_GUESSES

    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO wordle_guesses (user_id, game_date, guess_num, guess_word) VALUES (?, ?, ?, ?)",
            (user_id, today.isoformat(), guess_num, guess),
        )
        if game_over:
            conn.execute(
                "INSERT INTO wordle_results (user_id, game_date, won, guesses_used) VALUES (?, ?, ?, ?)",
                (user_id, today.isoformat(), 1 if won else 0, guess_num),
            )
        conn.commit()

    points_awarded = None
    if won:
        points_awarded = POINTS_BY_GUESS_COUNT.get(guess_num, 5)

    return {
        "valid": True,
        "error": "",
        "feedback": feedback,
        "won": won,
        "game_over": game_over,
        "guesses_used": guess_num,
        "answer": answer if game_over else None,
        "points_awarded": points_awarded,
    }
