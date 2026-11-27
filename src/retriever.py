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

    def retrieve_deep(self, question: str, doc_id: str | None = None,
                      top_k: int | None = None, max_chunks: int | None = None) -> list[dict]:
        """Gather everything the document says about a topic: take the best
        matches over a wide candidate pool, pull in the chunk before and
        after each one so explanations are not cut mid-thought, and return
        the result in reading order (document, then position)."""
        top_k = top_k or settings.deep_top_k
        max_chunks = max_chunks or settings.deep_max_chunks
        query_vector = self.engine.encode([question])
        pool = max(top_k * 6, 40)
        hits = self.store.search(query_vector, pool)
        if doc_id:
            hits = [h for h in hits if h["doc_id"] == doc_id]
        hits = [h for h in hits if h["score"] >= settings.deep_min_score]
        seeds = hits[:top_k]
        if not seeds:
            return []
        scores = {h["vid"]: h["score"] for h in hits}
        chosen: dict[int, dict] = {}
        for h in seeds:
            chosen[h["vid"]] = h
        # Expand with neighbours, best seeds first, until the cap.
        for h in seeds:
            for nid in self.store.neighbors(h["vid"], h["doc_id"], span=1):
                if len(chosen) >= max_chunks:
                    break
                if nid not in chosen:
                    meta = self.store.get(nid)
                    if meta:
                        meta["score"] = scores.get(nid, max(0.0, h["score"] - 0.05))
                        chosen[nid] = meta
        ordered = sorted(chosen.values(), key=lambda m: (m["doc_id"], m["vid"]))
        return ordered
