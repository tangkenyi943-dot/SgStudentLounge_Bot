"""
Content filter for confessions.
"""

import re

from better_profanity import profanity

profanity.load_censor_words()
_BASE_BLOCKLIST = {str(w).lower() for w in profanity.CENSOR_WORDSET}

ALLOWED_PROFANITY = {
    "damn", "hell", "ass", "asses", "asshole", "assholes",
    "shit", "shitty", "bullshit", "horseshit",
    "fuck", "fucking", "fucked", "fucker", "fuckers", "fuckup",
    "bitch", "bitches", "bitchy",
    "crap", "crappy",
    "piss", "pissed", "pissing",
    "bastard", "bastards",
    "dick", "dicks", "dickhead",
    "douche", "douchebag",
    "screw", "screwed",
    "suck", "sucks", "sucked",
    "idiot", "idiotic", "stupid", "dumbass",
    "wtf", "stfu", "omfg",
}

BLOCKED_TERMS = _BASE_BLOCKLIST - ALLOWED_PROFANITY

MAX_MESSAGE_LENGTH = 2000
MIN_MESSAGE_LENGTH = 1

MAX_CONSECUTIVE_REPEATED_CHARS = 12
URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)


def check_message(text: str) -> tuple[bool, str]:
    if not text or len(text.strip()) < MIN_MESSAGE_LENGTH:
        return False, "Your confession can't be empty."

    if len(text) > MAX_MESSAGE_LENGTH:
        return False, f"Confession is too long (max {MAX_MESSAGE_LENGTH} characters)."

    lowered = text.lower()
    words_in_text = set(re.findall(r"[a-z']+", lowered))

    if words_in_text & BLOCKED_TERMS:
        return False, "Your message contains content that isn't allowed here."

    if re.search(r"(.)\1{" + str(MAX_CONSECUTIVE_REPEATED_CHARS - 1) + ",}", text):
        return False, "Message looks like spam (too many repeated characters)."

    return True, ""
