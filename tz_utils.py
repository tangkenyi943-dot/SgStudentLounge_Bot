"""
Shared timezone helper.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")


def today_sgt() -> date:
    return datetime.now(SGT).date()


def now_sgt() -> datetime:
    return datetime.now(SGT)
