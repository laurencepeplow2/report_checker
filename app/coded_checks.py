"""Deterministic per-chunk checks that appear in Review alongside the AI
flags - no API calls. Currently: sentence length (config columns
sentence_word_flag / sentence_word_limit, e.g. amber > 12, red > 16 words).
"""
from __future__ import annotations

import re

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def sentence_length_flag(text: str, limits: dict[str, int]) -> tuple[str, str]:
    """Flag a paragraph by its longest sentence.

    limits maps flag letters to word limits, e.g. {"a": 12, "r": 16}:
    any sentence over the red limit -> "r", over amber -> "a", else "g".
    Returns (flag, detail) where detail names the longest sentence.
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
        return "g", f"longest sentence {longest_words} words"
    snippet = longest_sentence[:140] + ("..." if len(longest_sentence) > 140 else "")
    return flag, (f"{longest_words}-word sentence (amber > {amber}, "
                  f"red > {red}): \"{snippet}\"")
