"""
Basic content filter for confessions.
"""

import re

BLOCKED_TERMS = [
    # add your blocked words here, lowercase, e.g. "badword",
]

MAX_MESSAGE_LENGTH = 2000
MIN_MESSAGE_LENGTH = 1
MAX_CONSECUTIVE_REPEATED_CHARS = 12


def check_message(text: str) -> tuple[bool, str]:
    if not text or len(text.strip()) < MIN_MESSAGE_LENGTH:
        return False, "Your confession can't be empty."

    if len(text) > MAX_MESSAGE_LENGTH:
        return False, f"Confession is too long (max {MAX_MESSAGE_LENGTH} characters)."

    lowered = text.lower()

    for term in BLOCKED_TERMS:
        if term and term.lower() in lowered:
            return False, "Your message contains content that isn't allowed here."

    if re.search(r"(.)\1{" + str(MAX_CONSECUTIVE_REPEATED_CHARS - 1) + ",}", text):
        return False, "Message looks like spam (too many repeated characters)."

    return True, ""
