"""Embedding tests load the real model once per session (~90 MB download
on first run). They verify the math invariants the pipeline relies on."""
import numpy as np
import pytest

from src.config import settings
from src.embeddings import EmbeddingEngine


@pytest.fixture(scope="session")
def engine() -> EmbeddingEngine:
    return EmbeddingEngine()


class TestEncode:
    def test_output_shape_and_dtype(self, engine):
        v = engine.encode(["hello world", "another sentence"])
        assert v.shape == (2, settings.embedding_dim)
        assert v.dtype == np.float32
        assert v.flags["C_CONTIGUOUS"]

    def test_unit_norm(self, engine):
        v = engine.encode(["a short passage", "a much longer passage " * 20])
        norms = np.linalg.norm(v, axis=1)
        np.testing.assert_allclose(norms, 1.0, atol=1e-4)

    def test_empty_input(self, engine):
        v = engine.encode([])
        assert v.shape == (0, settings.embedding_dim)

    def test_semantic_ranking(self, engine):
        texts = [
            "FAISS indexes dense vectors for similarity search.",
            "The recipe calls for two cups of flour.",
        ]
        v = engine.encode(texts)
        q = engine.encode(["vector database retrieval"])[0]
        sims = v @ q
        assert sims[0] > sims[1]


class TestTokenCounter:
    def test_counts_with_model_tokenizer(self, engine):
        # MiniLM word-pieces: a sentence counts more tokens than words.
        n = engine.count_tokens("Tokenization splits words into pieces.")
        assert n >= 6
