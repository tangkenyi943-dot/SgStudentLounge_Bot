"""
Persistent storage for confession bot identities.

Each Telegram user gets exactly one row: their internal user_id maps to a
chosen username and a deterministically-assigned emoji avatar. The avatar
is derived from a hash of the username so it's stable and doesn't need its
own storage column, but we store it anyway for simplicity/clarity and so it
doesn't change if hashing logic ever changes.
"""

import hashlib
import re
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

from config import DB_DIR

DB_PATH = Path(DB_DIR) / "confessions.db"

# A reasonably sized pool of distinct, friendly emoji avatars.
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
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                banned      INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


def _avatar_for(username: str) -> str:
    """Deterministically pick an avatar based on the username's hash."""
    digest = hashlib.sha256(username.lower().encode("utf-8")).hexdigest()
    index = int(digest, 16) % len(AVATAR_POOL)
    return AVATAR_POOL[index]


def _generate_hex_id() -> str:
    """Generates a random 8-character uppercase hex ID, e.g. 'A1B2C3D4'."""
    return secrets.token_hex(4).upper()


def _unique_hex_id(conn) -> str:
    """Generates a hex ID, retrying on the rare collision."""
    for _ in range(10):
        candidate = _generate_hex_id()
        cur = conn.execute(
            "SELECT 1 FROM identities WHERE hex_id = ?", (candidate,)
        )
        if cur.fetchone() is None:
            return candidate
    # Astronomically unlikely to ever reach here (8 hex chars = 4+ billion
    # combinations), but fail loudly rather than silently using a collision.
    raise RuntimeError("Could not generate a unique hex ID after 10 attempts")


def validate_username(username: str) -> tuple[bool, str]:
    """Returns (is_valid, error_message). error_message is '' if valid."""
    username = username.strip()
    if len(username) < USERNAME_MIN_LEN:
        return False, "Username can't be empty."
    if len(username) > USERNAME_MAX_LEN:
        return False, f"Username must be {USERNAME_MAX_LEN} characters or fewer."
    # Loose rule per spec: just block empty/too long. We still strip control
    # chars and newlines since those would break the channel post formatting.
    if "\n" in username or "\r" in username:
        return False, "Username can't contain line breaks."
    return True, ""


def get_identity(user_id: int) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT * FROM identities WHERE user_id = ?", (user_id,)
        )
        return cur.fetchone()


def get_by_hex_id(hex_id: str) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT * FROM identities WHERE hex_id = ?", (hex_id.strip().upper(),)
        )
        return cur.fetchone()


def is_banned(user_id: int) -> bool:
    identity = get_identity(user_id)
    return bool(identity and identity["banned"])


def set_banned(hex_id: str, banned: bool) -> bool:
    """Returns True if a matching identity was found and updated, False otherwise."""
    with closing(_connect()) as conn:
        cur = conn.execute(
            "UPDATE identities SET banned = ? WHERE hex_id = ?",
            (1 if banned else 0, hex_id.strip().upper()),
        )
        conn.commit()
        return cur.rowcount > 0


def list_all_identities() -> list[sqlite3.Row]:
    """For admin use only — returns every user's real Telegram ID alongside their pseudonym."""
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT * FROM identities ORDER BY created_at ASC"
        )
        return cur.fetchall()


def username_taken(username: str, exclude_user_id: int | None = None) -> bool:
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


def set_identity(user_id: int, username: str) -> tuple[str, str]:
    """
    Creates or updates a user's identity. Returns (avatar, hex_id).
    The hex_id is generated once on first creation and never changes,
    even if the username is changed later — it's the permanent,
    impersonation-resistant identifier.
    """
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
