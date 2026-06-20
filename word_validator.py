"""
Word validation for Wordle guesses.
"""

from english_words import get_english_words_set

from wordle_game import WORD_BANK

_dictionary_words = {
    w.upper() for w in get_english_words_set(["web2"], lower=True)
    if len(w) == 5 and w.isalpha()
}

VALID_GUESSES = _dictionary_words | set(WORD_BANK)


def is_valid_word(word: str) -> bool:
    return word.strip().upper() in VALID_GUESSES
