"""Embedding wrapper (all-MiniLM-L6-v2 via ONNX runtime): lazy load, L2-normalized float32 vectors."""
from __future__ import annotations

import gc
import threading

import numpy as np

from .config import settings
from .utils import get_logger

logger = get_logger(__name__)


class EmbeddingEngine:
    """Loads all-MiniLM-L6-v2 on first use (keeps app startup fast and
    keeps the ~90 MB model out of memory for tests that never embed)."""

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or settings.embedding_model
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from fastembed import TextEmbedding

                    logger.info("Loading embedding model %s (ONNX runtime)", self.model_name)
                    self._model = TextEmbedding(self.model_name, threads=1)
        return self._model

    @property
    def max_seq_length(self) -> int:
        return 256

    def count_tokens(self, text: str) -> int:
        """Token count using the embedding model's own tokenizer, so chunk
        sizing matches what the model actually sees before truncation."""
        tokenizer = self._load().model.tokenizer
        return len(tokenizer.encode(text, add_special_tokens=False).tokens)

    def encode(self, texts: list[str], batch_size: int = 8) -> np.ndarray:
        """Encode to a contiguous (N, dim) float32 matrix of unit vectors.

        normalize_embeddings=True makes the inner product equal to exact
        cosine similarity, which is what FAISS IndexFlatIP searches.
        """
        if not texts:
            return np.zeros((0, settings.embedding_dim), dtype=np.float32)
        model = self._load()
        # Encode in small slices to keep peak memory low (512 MB free tier).
        parts = []
        for i in range(0, len(texts), 32):
            parts.append(
                np.asarray(list(model.embed(texts[i : i + 32], batch_size=batch_size)))
            )
            gc.collect()
        vectors = np.concatenate(parts, axis=0)
        vectors = vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
        vectors = np.ascontiguousarray(vectors, dtype=np.float32)
        if vectors.shape[1] != settings.embedding_dim:
            raise ValueError(
                f"Embedding dim {vectors.shape[1]} != expected {settings.embedding_dim}"
            )
        norms = np.linalg.norm(vectors, axis=1)
        if not np.allclose(norms, 1.0, atol=1e-4):
            raise ValueError("Embedding rows are not unit-normalized")
        return vectors
