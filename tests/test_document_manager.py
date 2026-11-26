"""Ingest, dedupe and delete with a deterministic fake embedding engine,
so the suite stays fast and offline. Embedding math is covered separately
in test_embeddings.py with the real model."""
import numpy as np
import pytest

from src.document_manager import (
    DocumentManager,
    DuplicateDocumentError,
)
from src.pdf_processor import PDFValidationError
from src.vector_store import VectorStore


class FakeEngine:
    model_name = "fake"

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    def encode(self, texts):
        rows = []
        for i, _ in enumerate(texts):
            v = np.zeros(384, dtype=np.float32)
            v[i % 384] = 1.0
            rows.append(v)
        return np.array(rows, dtype=np.float32)


@pytest.fixture
def manager(tmp_path):
    store = VectorStore(dim=384)
    return DocumentManager(FakeEngine(), store, registry_dir=tmp_path)


class TestIngest:
    def test_ingest_indexes_document(self, manager, two_page_pdf):
        record = manager.ingest(two_page_pdf, "notes.pdf")
        assert record["page_count"] == 2
        assert record["chunk_count"] >= 1
        assert len(record["doc_id"]) == 64
        assert manager.store.size == record["chunk_count"]
        assert manager.list_documents()[0]["doc_name"] == "notes.pdf"

    def test_duplicate_rejected(self, manager, one_page_pdf):
        manager.ingest(one_page_pdf, "a.pdf")
        with pytest.raises(DuplicateDocumentError):
            manager.ingest(one_page_pdf, "a-copy.pdf")

    def test_invalid_pdf_rejected(self, manager):
        with pytest.raises(PDFValidationError):
            manager.ingest(b"%PDF- broken", "bad.pdf")

    def test_registry_persists(self, manager, one_page_pdf, tmp_path):
        manager.ingest(one_page_pdf, "a.pdf")
        reloaded = DocumentManager(FakeEngine(), manager.store, registry_dir=tmp_path)
        assert len(reloaded.list_documents()) == 1


class TestDelete:
    def test_delete_removes_doc_and_vectors(self, manager, two_page_pdf):
        record = manager.ingest(two_page_pdf, "notes.pdf")
        assert manager.delete(record["doc_id"]) is True
        assert manager.store.size == 0
        assert manager.list_documents() == []
        assert manager.delete(record["doc_id"]) is False
