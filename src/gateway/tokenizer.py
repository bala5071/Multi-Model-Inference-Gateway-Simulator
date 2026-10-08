"""Tokenizer interface and a deterministic stand-in."""

from __future__ import annotations

import hashlib
from typing import Protocol


class Tokenizer(Protocol):
    def encode(self, text: str) -> tuple[int, ...]: ...


class WordTokenizer:
    """One token per whitespace-separated word.

    A word's id is a hash of its text, so the same word always gets the same id, in any
    process, with no vocabulary to build or share. Case and punctuation are significant.
    """

    def encode(self, text: str) -> tuple[int, ...]:
        return tuple(_word_id(word) for word in text.split())


def _word_id(word: str) -> int:
    return int.from_bytes(hashlib.blake2b(word.encode(), digest_size=8).digest(), "big")
