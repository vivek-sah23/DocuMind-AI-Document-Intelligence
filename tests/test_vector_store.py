import numpy as np
import pytest

from src.vector_store import VectorStore


def unit(v):
    v = np.array(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def meta(doc_id, n):
    return {
        "doc_id": doc_id,
        "doc_name": f"{doc_id}.pdf",
        "page_number": n,
        "chunk_id": f"{doc_id}-{n}",
        "text": f"chunk {n} of {doc_id}",
        "token_count": 10,
    }


@pytest.fixture
def store():
    return VectorStore(dim=3)


class TestAddSearch:
    def test_add_then_search_returns_metadata(self, store):
        store.add(np.array([unit([1, 0, 0])]), [meta("docA", 1)])
        hits = store.search(unit([1, 0, 0]), top_k=1)
        assert len(hits) == 1
        assert hits[0]["doc_id"] == "docA"
        assert hits[0]["score"] == pytest.approx(1.0, abs=1e-5)

    def test_search_ranks_by_inner_product(self, store):
        store.add(
            np.array([unit([1, 0, 0]), unit([0, 1, 0])]),
            [meta("docA", 1), meta("docB", 2)],
        )
        hits = store.search(unit([0.9, 0.1, 0]), top_k=2)
        assert hits[0]["doc_id"] == "docA"
        assert hits[1]["score"] < hits[0]["score"]

    def test_empty_store_returns_no_hits(self, store):
        assert store.search(unit([1, 0, 0]), top_k=5) == []


class TestDeletion:
    def test_remove_doc_deletes_only_that_doc(self, store):
        store.add(
            np.array([unit([1, 0, 0]), unit([0, 1, 0]), unit([0, 0, 1])]),
            [meta("docA", 1), meta("docB", 2), meta("docA", 3)],
        )
        removed = store.remove_doc("docA")
        assert removed == 2
        assert store.size == 1
        hits = store.search(unit([1, 0, 0]), top_k=5)
        assert all(h["doc_id"] == "docB" for h in hits)

    def test_remove_missing_doc_returns_zero(self, store):
        store.add(np.array([unit([1, 0, 0])]), [meta("docA", 1)])
        assert store.remove_doc("nope") == 0


class TestPersistence:
    def test_save_load_round_trip(self, store, tmp_path):
        store.add(
            np.array([unit([1, 0, 0]), unit([0, 1, 0])]),
            [meta("docA", 1), meta("docB", 2)],
        )
        store.save(tmp_path)

        loaded = VectorStore.load(tmp_path)
        assert loaded.size == 2
        hits = loaded.search(unit([0, 1, 0]), top_k=1)
        assert hits[0]["doc_id"] == "docB"
        assert hits[0]["page_number"] == 2

        # New ids must not collide with reloaded ones.
        loaded.add(np.array([unit([0, 0, 1])]), [meta("docC", 1)])
        assert loaded.size == 3

    def test_load_missing_dir_returns_empty_store(self, tmp_path):
        loaded = VectorStore.load(tmp_path / "nothing")
        assert loaded.size == 0
