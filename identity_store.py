"""
Persistent storage for confession bot identities.

Each Telegram user gets exactly one row: their internal user_id maps to a
chosen username, a deterministically-assigned emoji avatar, and a
permanent random hex ID.
"""

import hashlib
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

DB_PATH = Path(__file__).parent / "confessions.db"

AVATAR_POOL = [
    "🦊", "🐺", "🦁", "🐯", "🐨", "🐼", "🐸", "🐙", "🦉", "🦅",
    "🐢", "🦋", "🐳", "🦄", "🐝", "🦔", "🦦", "🦝", "🐧", "🦓",
    "🦒", "🐠", "🦜", "🦢", "🐲", "🌵", "🍄", "🌙", "⭐", "🔥",
]

USERNAME_MAX_LEN = 30
USERNAME_MIN_LEN = 1


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS identities (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT NOT NULL UNIQUE,
                avatar      TEXT NOT NULL,
                hex_id      TEXT NOT NULL UNIQUE,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def _avatar_for(username: str) -> str:
    digest = hashlib.sha256(username.lower().encode("utf-8")).hexdigest()
    index = int(digest, 16) % len(AVATAR_POOL)
    return AVATAR_POOL[index]


def _generate_hex_id() -> str:
    return secrets.token_hex(4).upper()


def _unique_hex_id(conn) -> str:
    for _ in range(10):
        candidate = _generate_hex_id()
        cur = conn.execute(
            "SELECT 1 FROM identities WHERE hex_id = ?", (candidate,)
        )
        if cur.fetchone() is None:
            return candidate
    raise RuntimeError("Could not generate a unique hex ID after 10 attempts")


def validate_username(username: str) -> tuple[bool, str]:
    username = username.strip()
    if len(username) < USERNAME_MIN_LEN:
        return False, "Username can't be empty."
    if len(username) > USERNAME_MAX_LEN:
        return False, f"Username must be {USERNAME_MAX_LEN} characters or fewer."
    if "\n" in username or "\r" in username:
        return False, "Username can't contain line breaks."
    return True, ""


def get_identity(user_id: int):
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT * FROM identities WHERE user_id = ?", (user_id,)
        )
        return cur.fetchone()


def username_taken(username: str, exclude_user_id: int = None) -> bool:
    with closing(_connect()) as conn:
        if exclude_user_id is not None:
            cur = conn.execute(
                "SELECT 1 FROM identities WHERE LOWER(username) = LOWER(?) AND user_id != ?",
                (username, exclude_user_id),
            )
        else:
            cur = conn.execute(
                "SELECT 1 FROM identities WHERE LOWER(username) = LOWER(?)",
                (username,),
            )
        return cur.fetchone() is not None


def set_identity(user_id: int, username: str):
    avatar = _avatar_for(username)
    with closing(_connect()) as conn:
        existing = conn.execute(
            "SELECT hex_id FROM identities WHERE user_id = ?", (user_id,)
        ).fetchone()

        if existing:
            hex_id = existing["hex_id"]
            conn.execute(
                "UPDATE identities SET username = ?, avatar = ? WHERE user_id = ?",
                (username, avatar, user_id),
            )
        else:
            hex_id = _unique_hex_id(conn)
            conn.execute(
                "INSERT INTO identities (user_id, username, avatar, hex_id) VALUES (?, ?, ?, ?)",
                (user_id, username, avatar, hex_id),
            )
        conn.commit()
    return avatar, hex_id
