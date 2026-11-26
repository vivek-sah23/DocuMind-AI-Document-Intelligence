"""Query embedding, top-k search and relevance thresholding."""
from __future__ import annotations

from .config import settings
from .embeddings import EmbeddingEngine
from .vector_store import VectorStore


class Retriever:
    def __init__(self, engine: EmbeddingEngine, store: VectorStore) -> None:
        self.engine = engine
        self.store = store

    def retrieve(
        self,
        question: str,
        top_k: int | None = None,
        min_score: float | None = None,
        doc_id: str | None = None,
    ) -> list[dict]:
        """Return hits ranked by cosine similarity, filtered by the
        relevance threshold so irrelevant questions get a safe refusal
        instead of a forced answer."""
        top_k = top_k if top_k is not None else settings.top_k
        min_score = (
            min_score if min_score is not None else settings.min_similarity_score
        )
        query_vector = self.engine.encode([question])
        # Fetch extra candidates when filtering to one document.
        fetch_k = top_k * 4 if doc_id else top_k
        hits = self.store.search(query_vector, fetch_k)
        if doc_id:
            hits = [h for h in hits if h["doc_id"] == doc_id]
        hits = [h for h in hits if h["score"] >= min_score]
        return hits[:top_k]
