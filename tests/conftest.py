"""Shared fixtures: tiny PDFs built in memory with PyMuPDF."""
from __future__ import annotations

import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LOREM_PAGE_1 = (
    "The retrieval pipeline indexes documents offline. "
    "Each page is extracted with PyMuPDF and cleaned. "
    "Sentences are packed into overlapping windows. "
    "The FAISS index stores unit-normalized vectors."
)
LOREM_PAGE_2 = (
    "Cosine similarity ranks passages for a question. "
    "Low scoring results trigger a safe refusal. "
    "Citations always resolve from stored metadata. "
    "The model never invents page numbers, e.g. page 3 or Fig. 2."
)


def make_pdf(pages: list[str]) -> bytes:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def one_page_pdf() -> bytes:
    return make_pdf([LOREM_PAGE_1])


@pytest.fixture
def two_page_pdf() -> bytes:
    return make_pdf([LOREM_PAGE_1, LOREM_PAGE_2])


@pytest.fixture
def blank_pdf() -> bytes:
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data
