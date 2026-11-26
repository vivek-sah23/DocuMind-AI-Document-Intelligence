"""Relevant vs irrelevant queries against a small real index."""
import numpy as np
import pytest

from src.embeddings import EmbeddingEngine
from src.retriever import Retriever
from src.vector_store import VectorStore

CHUNKS = [
    {
        "doc_id": "nist",
        "doc_name": "nist_csf_2.0.pdf",
        "page_number": 5,
        "chunk_id": "nist-00001",
        "text": (
            "The CSF 2.0 functions are Govern, Identify, Protect, Detect, "
            "Respond and Recover. They organize cybersecurity outcomes."
        ),
        "token_count": 24,
    },
    {
        "doc_id": "nasa",
        "doc_name": "nasa_handbook.pdf",
        "page_number": 42,
        "chunk_id": "nasa-00042",
        "text": (
            "Systems engineering trade studies compare candidate designs "
            "against cost, schedule and performance criteria."
        ),
        "token_count": 18,
    },
]


@pytest.fixture(scope="session")
def retriever() -> Retriever:
    engine = EmbeddingEngine()
    store = VectorStore(dim=384)
    store.add(engine.encode([c["text"] for c in CHUNKS]), CHUNKS)
    return Retriever(engine, store)


class TestRetrieval:
    def test_relevant_query_hits_right_document(self, retriever):
        hits = retriever.retrieve("What are the CSF functions?", min_score=0.0)
        assert hits
        assert hits[0]["doc_name"] == "nist_csf_2.0.pdf"
        assert hits[0]["page_number"] == 5

    def test_irrelevant_query_filtered_by_threshold(self, retriever):
        hits = retriever.retrieve(
            "How do I bake sourdough bread?", min_score=0.55
        )
        assert hits == []

    def test_threshold_zero_returns_up_to_top_k(self, retriever):
        hits = retriever.retrieve("anything at all", top_k=2, min_score=0.0)
        assert 1 <= len(hits) <= 2

    def test_doc_filter(self, retriever):
        hits = retriever.retrieve(
            "design trade studies", min_score=0.0, doc_id="nasa"
        )
        assert hits and all(h["doc_id"] == "nasa" for h in hits)
