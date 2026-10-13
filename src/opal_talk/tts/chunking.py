"""Internal text chunking. Chunk boundaries are never exposed to API clients."""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"(?<=[.!?…;:])\s+|\n\s*\n|\n")
_CLAUSE_END = re.compile(r"(?<=[,;:—–])\s+")


def _split_long(piece: str, max_chars: int) -> list[str]:
    if len(piece) <= max_chars:
        return [piece]
    out: list[str] = []
    for clause in _CLAUSE_END.split(piece):
        if len(clause) <= max_chars:
            out.append(clause)
            continue
        current = ""
        for word in clause.split():
            while len(word) > max_chars:
                if current:
                    out.append(current)
                    current = ""
                out.append(word[:max_chars])
                word = word[max_chars:]
            candidate = f"{current} {word}" if current else word
            if len(candidate) > max_chars:
                out.append(current)
                current = word
            else:
                current = candidate
        if current:
            out.append(current)
    return out


def chunk_text(text: str, max_chars: int = 400) -> list[str]:
    """Split plain text into ordered chunks of at most `max_chars`, preferring sentence boundaries."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    pieces: list[str] = []
    for sentence in _SENTENCE_END.split(text):
        sentence = " ".join(sentence.split())
        if sentence:
            pieces.extend(_split_long(sentence, max_chars))

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current} {piece}" if current else piece
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks
