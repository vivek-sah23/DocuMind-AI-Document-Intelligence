"""Sentence-aware, token-bounded chunking with overlap.

Correction history:
- Chunks are sized to ~200 tokens with 40 overlap so they fit inside the
  embedding model's 256-token truncation window (a 500-token chunk would
  silently lose its second half in the embedding).
- Token counting is injected from the embedding model's own tokenizer;
  a whitespace estimator is only a fallback for tests.
- The naive split regex (?<=[.!?])\\s+ breaks on abbreviations, versions
  and decimals ("e.g.", "Fig. 3", "v2.1", "3.14"); they are guarded here.
- A single sentence longer than the chunk window is hard-split so the
  packing loop can never stall.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .pdf_processor import PageText

TokenCounter = Callable[[str], int]

# Abbreviations whose trailing period must not end a sentence.
_ABBREVIATIONS = [
    "e.g.", "i.e.", "etc.", "vs.", "cf.", "al.", "Fig.", "Figs.", "Eq.",
    "Dr.", "Mr.", "Mrs.", "Ms.", "Prof.", "Sr.", "Jr.", "St.", "No.",
    "Nos.", "Vol.", "pp.", "ed.", "Inc.", "Ltd.", "Corp.", "Dept.",
    "approx.", "min.", "max.", "sec.", "resp.",
]
_PLACEHOLDER = "\x00"  # never appears in real PDF text

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'\(\[\u201c\u2018])")


def estimate_tokens(text: str) -> int:
    """Fallback counter: words plus punctuation, ~1 token each."""
    return len(re.findall(r"\w+|[^\w\s]", text))


def split_sentences(text: str) -> list[str]:
    """Split into sentences without breaking abbreviations or numbers."""
    protected = text
    for abbr in _ABBREVIATIONS:
        protected = protected.replace(abbr, abbr.replace(".", _PLACEHOLDER))
    # Decimals and version strings: 3.14, v2.1, 1.2.3
    protected = re.sub(r"(?<=\d)\.(?=\d)", _PLACEHOLDER, protected)
    parts = _SENTENCE_SPLIT.split(protected)
    return [
        p.replace(_PLACEHOLDER, ".").strip()
        for p in parts
        if p.replace(_PLACEHOLDER, ".").strip()
    ]


@dataclass
class Chunk:
    doc_id: str
    doc_name: str
    page_number: int
    chunk_id: str
    text: str
    token_count: int

    def to_metadata(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "doc_name": self.doc_name,
            "page_number": self.page_number,
            "chunk_id": self.chunk_id,
            "text": self.text,
            "token_count": self.token_count,
        }


def _hard_split(sentence: str, limit: int, count: TokenCounter) -> list[str]:
    """Split an over-long sentence into <= limit-token pieces on word
    boundaries so the packing loop always makes progress."""
    words = sentence.split()
    pieces, current = [], []
    for word in words:
        candidate = " ".join(current + [word])
        if current and count(candidate) > limit:
            pieces.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        pieces.append(" ".join(current))
    return pieces


def chunk_pages(
    pages: list[PageText],
    doc_id: str,
    doc_name: str,
    chunk_size: int,
    overlap: int,
    count_tokens: TokenCounter = estimate_tokens,
) -> list[Chunk]:
    """Pack sentences into token windows with a trailing carry-over.

    Overlap is sentence-level: the tail sentences of a window whose
    combined size is closest to (and at most) `overlap` tokens are
    prepended to the next window.
    """
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")

    units: list[tuple[int, str, int]] = []  # (page_number, sentence, tokens)
    for page in pages:
        if not page.text:
            continue
        for sentence in split_sentences(page.text):
            tokens = count_tokens(sentence)
            if tokens > chunk_size:
                for piece in _hard_split(sentence, chunk_size, count_tokens):
                    units.append((page.page_number, piece, count_tokens(piece)))
            else:
                units.append((page.page_number, sentence, tokens))

    chunks: list[Chunk] = []
    window: list[tuple[int, str, int]] = []
    window_tokens = 0
    seq = 0

    def flush() -> None:
        nonlocal window, window_tokens, seq
        if not window:
            return
        text = " ".join(s for _, s, _ in window)
        chunks.append(
            Chunk(
                doc_id=doc_id,
                doc_name=doc_name,
                page_number=window[0][0],
                chunk_id=f"{doc_id[:12]}-{seq:05d}",
                text=text,
                token_count=window_tokens,
            )
        )
        seq += 1
        # Carry over tail sentences up to the overlap budget.
        carry: list[tuple[int, str, int]] = []
        carry_tokens = 0
        for unit in reversed(window):
            if carry_tokens + unit[2] > overlap:
                break
            carry.insert(0, unit)
            carry_tokens += unit[2]
        window = carry
        window_tokens = carry_tokens

    for unit in units:
        if window and window[-1][0] != unit[0]:
            # Never pack across a page boundary: a chunk keeps one page
            # number, so every citation opens the page the text came from.
            flush()
            window, window_tokens = [], 0
        if window and window_tokens + unit[2] > chunk_size:
            flush()
        window.append(unit)
        window_tokens += unit[2]
    flush()
    return chunks
