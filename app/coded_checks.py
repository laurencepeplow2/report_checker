"""Deterministic per-chunk checks that appear in Review alongside the AI
flags - no API calls. Currently: sentence length (config columns
sentence_word_flag / sentence_word_limit, e.g. amber > 12, red > 16 words).
"""
from __future__ import annotations

import re

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

NUMBER_WORDS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty",
    "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "million",
    "billion", "trillion",
}
_DIGIT_RE = re.compile(r"\d")
_WORD_RE = re.compile(r"[a-z]+")

# "Transport & Environment" / "Transport and Environment" in full
ORG_FULL_NAME_RE = re.compile(r"Transport\s*(?:&|and)\s*Environment", re.IGNORECASE)


def contains_number(text: str) -> bool:
    """True if the text has a numeral (1, 100.1, 10,000) or a number word."""
    if _DIGIT_RE.search(text):
        return True
    return any(w in NUMBER_WORDS for w in _WORD_RE.findall(text.lower()))


def org_full_name_flag(text: str) -> tuple[str, str, str]:
    """Flag use of the organisation's full name (should always be "T&E").
    Returns (flag, detail, quote)."""
    m = ORG_FULL_NAME_RE.search(text)
    if not m:
        return "g", "uses T&E (or no organisation name)", ""
    return "r", 'uses the full organisation name instead of "T&E"', m.group(0)


def sentence_length_flag(text: str, limits: dict[str, int]) -> tuple[str, str, str]:
    """Flag a paragraph by its longest sentence.

    limits maps flag letters to word limits, e.g. {"a": 12, "r": 16}:
    any sentence over the red limit -> "r", over amber -> "a", else "g".
    Returns (flag, detail, sentence) - detail is a human note, sentence is
    the verbatim longest sentence (for highlighting in the extract).
    """
    amber = limits.get("a", 0)
    red = limits.get("r", 0)
    longest_words = 0
    longest_sentence = ""
    for sentence in SENTENCE_SPLIT_RE.split(text):
        words = len(sentence.split())
        if words > longest_words:
            longest_words = words
            longest_sentence = sentence.strip()

    if red and longest_words > red:
        flag = "r"
    elif amber and longest_words > amber:
        flag = "a"
    else:
        return "g", f"longest sentence {longest_words} words", ""
    detail = (f"{longest_words}-word sentence (amber > {amber}, red > {red})")
    return flag, detail, longest_sentence
