"""
Fishing game.
"""

import random
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from tz_utils import now_sgt, today_sgt

DB_PATH = Path(__file__).parent / "confessions.db"
MEDIA_DIR = Path(__file__).parent / "media" / "fish"

GAME_NAME = "fishing"

MAX_CASTS_PER_DAY = 20
COOLDOWN_MINUTES = 30
REEL_WAIT_SECONDS = 10

RARITY_WEIGHTS = {
    "common": 70.0,
    "rare": 25.0,
    "legendary": 4.5,
    "secret": 0.5,
}

RARITY_LABELS = {
    "common": "Common",
    "rare": "✨ Rare",
    "legendary": "🌟 Legendary",
    "secret": "💎 SECRET",
}

FISH_CATALOG = {
    "business_pup": ("Business Pup", "🐶", "common", 8, None),
    "giga_mouse": ("Giga Mouse", "🐭", "common", 6, None),
    "six_and_seven": ("SIX&7", "🐱", "common", 7, None),
    "pond_carp": ("Pond Carp", "🐟", "common", 5, None),
    "old_boot": ("Old Boot", "🥾", "common", 5, None),
    "seaweed_clump": ("Seaweed Clump", "🌿", "common", 5, None),
    "tin_can": ("Rusty Tin Can", "🥫", "common", 5, None),
    "tiny_crab": ("Tiny Crab", "🦀", "common", 8, None),
    "banana_cat": ("Banana Cat", "🍌", "rare", 25, None),
    "soju_monster": ("Soju Monster", "🍶", "rare", 30, None),
    "spicy_noodles": ("Mac, Cheese & Bang Bang Sauce", "🌶️", "rare", 28, None),
    "hungry_cat": ("Hungry Cat", "😼", "rare", 22, None),
    "golden_koi": ("Golden Koi", "🐠", "rare", 35, None),
    "ancient_eel": ("Ancient Eel", "🐍", "legendary", 90, None),
    "kraken_jr": ("Kraken Jr.", "🐙", "legendary", 110, None),
    "moonlit_shark": ("Moonlit Shark", "🦈", "legendary", 130, None),
    "mythic_whale": ("The Mythic Whale", "🐳", "secret", 300, None),
}


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_fishing_db() -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fishing_casts (
                cast_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                cast_date   TEXT NOT NULL,
                cast_time   TEXT NOT NULL,
                resolved    INTEGER NOT NULL DEFAULT 0,
                fish_key    TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fish_tank (
                user_id     INTEGER NOT NULL,
                fish_key    TEXT NOT NULL,
                catch_count INTEGER NOT NULL DEFAULT 0,
                first_caught_at TEXT,
                PRIMARY KEY (user_id, fish_key)
            )
            """
        )
        conn.commit()


def _today_cast_count(user_id: int, today_str: str) -> int:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS c FROM fishing_casts WHERE user_id = ? AND cast_date = ?",
            (user_id, today_str),
        )
        return cur.fetchone()["c"]


def _last_cast_time(user_id: int):
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT cast_time FROM fishing_casts WHERE user_id = ? ORDER BY cast_id DESC LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["cast_time"])


def get_pending_cast(user_id: int):
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT * FROM fishing_casts WHERE user_id = ? AND resolved = 0 ORDER BY cast_id DESC LIMIT 1",
            (user_id,),
        )
        return cur.fetchone()


def start_cast(user_id: int, now=None) -> dict:
    now = now or now_sgt()
    today_str = now.date().isoformat()

    if get_pending_cast(user_id) is not None:
        return {"ok": False, "error": "You already have a line in the water! Wait for it, then /reel.", "cast_id": None}

    casts_today = _today_cast_count(user_id, today_str)
    if casts_today >= MAX_CASTS_PER_DAY:
        return {"ok": False, "error": f"You've used all {MAX_CASTS_PER_DAY} casts for today. Come back tomorrow!", "cast_id": None}

    last_cast = _last_cast_time(user_id)
    if last_cast is not None:
        elapsed = now - last_cast
        cooldown = timedelta(minutes=COOLDOWN_MINUTES)
        if elapsed < cooldown:
            remaining = cooldown - elapsed
            minutes_left = int(remaining.total_seconds() // 60) + 1
            return {"ok": False, "error": f"Your rod needs a break — try again in about {minutes_left} more minute(s).", "cast_id": None}

    with closing(_connect()) as conn:
        cur = conn.execute(
            "INSERT INTO fishing_casts (user_id, cast_date, cast_time, resolved) VALUES (?, ?, ?, 0)",
            (user_id, today_str, now.isoformat()),
        )
        conn.commit()
        cast_id = cur.lastrowid

    return {"ok": True, "error": "", "cast_id": cast_id, "wait_seconds": REEL_WAIT_SECONDS}


def _roll_fish() -> str:
    rarity_roll = random.choices(
        list(RARITY_WEIGHTS.keys()), weights=list(RARITY_WEIGHTS.values()), k=1
    )[0]
    candidates = [key for key, data in FISH_CATALOG.items() if data[2] == rarity_roll]
    return random.choice(candidates)


def reel_in(user_id: int, now=None) -> dict:
    now = now or now_sgt()
    pending = get_pending_cast(user_id)

    if pending is None:
        return {"ok": False, "error": "You don't have a line in the water. Use /fish to cast first!"}

    cast_time = datetime.fromisoformat(pending["cast_time"])
    elapsed = (now - cast_time).total_seconds()
    if elapsed < REEL_WAIT_SECONDS:
        remaining = int(REEL_WAIT_SECONDS - elapsed) + 1
        return {"ok": False, "error": f"Not yet! Wait {remaining} more second(s) before reeling in."}

    fish_key = _roll_fish()
    name, emoji, rarity, points, media_file = FISH_CATALOG[fish_key]

    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE fishing_casts SET resolved = 1, fish_key = ? WHERE cast_id = ?",
            (fish_key, pending["cast_id"]),
        )

        existing = conn.execute(
            "SELECT catch_count FROM fish_tank WHERE user_id = ? AND fish_key = ?",
            (user_id, fish_key),
        ).fetchone()
        is_new = existing is None

        if is_new:
            conn.execute(
                "INSERT INTO fish_tank (user_id, fish_key, catch_count, first_caught_at) VALUES (?, ?, 1, ?)",
                (user_id, fish_key, now.isoformat()),
            )
        else:
            conn.execute(
                "UPDATE fish_tank SET catch_count = catch_count + 1 WHERE user_id = ? AND fish_key = ?",
                (user_id, fish_key),
            )
        conn.commit()

    return {
        "ok": True,
        "error": "",
        "fish_key": fish_key,
        "name": name,
        "emoji": emoji,
        "rarity": rarity,
        "rarity_label": RARITY_LABELS[rarity],
        "points": points,
        "media_file": media_file,
        "is_new": is_new,
    }


def get_tank(user_id: int):
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT fish_key, catch_count, first_caught_at FROM fish_tank WHERE user_id = ? ORDER BY first_caught_at ASC",
            (user_id,),
        )
        return cur.fetchall()


def get_casts_remaining_today(user_id: int, today=None) -> int:
    today_str = (today or today_sgt()).isoformat()
    used = _today_cast_count(user_id, today_str)
    return max(0, MAX_CASTS_PER_DAY - used)
