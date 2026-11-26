"""FAISS IndexFlatIP (wrapped in IndexIDMap2) plus JSON metadata.

Correction history:
- IndexIDMap2 needs int64 vector ids, but doc_id is a SHA-256 string.
  We keep a monotonically increasing int64 id per vector and a separate
  JSON map from that int id to the chunk metadata (which carries doc_id).
- Persistence is JSON + faiss.write_index only. No pickle anywhere, so a
  tampered metadata file can never execute code on load.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import faiss
import numpy as np

from .config import settings
from .utils import get_logger

logger = get_logger(__name__)

INDEX_FILE = "documind.faiss"
METADATA_FILE = "chunks.json"


class VectorStore:
    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim or settings.embedding_dim
        self._index: faiss.IndexIDMap2 = faiss.IndexIDMap2(
            faiss.IndexFlatIP(self.dim)
        )
        self._next_id = 0
        self._meta: dict[int, dict] = {}
        self._lock = threading.Lock()

    # -- writes ---------------------------------------------------------
    def add(self, vectors: np.ndarray, metadatas: list[dict]) -> list[int]:
        if vectors.shape[0] != len(metadatas):
            raise ValueError("vectors and metadatas length mismatch")
        if vectors.shape[0] == 0:
            return []
        vectors = np.ascontiguousarray(vectors, dtype=np.float32)
        with self._lock:
            ids = np.arange(
                self._next_id, self._next_id + len(metadatas), dtype=np.int64
            )
            self._index.add_with_ids(vectors, ids)
            for int_id, meta in zip(ids.tolist(), metadatas):
                self._meta[int_id] = meta
            self._next_id += len(metadatas)
        return ids.tolist()

    def remove_doc(self, doc_id: str) -> int:
        """Delete every vector of one document by its int64 ids."""
        with self._lock:
            doomed = [
                int_id
                for int_id, meta in self._meta.items()
                if meta["doc_id"] == doc_id
            ]
            if not doomed:
                return 0
            self._index.remove_ids(np.array(doomed, dtype=np.int64))
            for int_id in doomed:
                del self._meta[int_id]
        return len(doomed)

    # -- reads ----------------------------------------------------------
    @property
    def size(self) -> int:
        return self._index.ntotal

    def search(self, query_vector: np.ndarray, top_k: int) -> list[dict]:
        if self.size == 0:
            return []
        q = np.ascontiguousarray(query_vector, dtype=np.float32).reshape(1, -1)
        scores, ids = self._index.search(q, min(top_k, self.size))
        hits: list[dict] = []
        for score, int_id in zip(scores[0].tolist(), ids[0].tolist()):
            if int_id == -1:  # FAISS padding when fewer than k vectors
                continue
            meta = self._meta.get(int_id)
            if meta is None:
                continue
            hits.append({"score": float(score), **meta})
        return hits

    # -- persistence ----------------------------------------------------
    def save(self, directory: Path | None = None) -> None:
        directory = Path(directory) if directory else settings.index_dir
        directory.mkdir(parents=True, exist_ok=True)
        with self._lock:
            faiss.write_index(self._index, str(directory / INDEX_FILE))
            payload = {
                "dim": self.dim,
                "next_id": self._next_id,
                "entries": [
                    {"id": int_id, **meta}
                    for int_id, meta in sorted(self._meta.items())
                ],
            }
            (directory / METADATA_FILE).write_text(
                json.dumps(payload, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )

    @classmethod
    def load(cls, directory: Path | None = None) -> "VectorStore":
        directory = Path(directory) if directory else settings.index_dir
        index_path = directory / INDEX_FILE
        meta_path = directory / METADATA_FILE
        if not index_path.exists() or not meta_path.exists():
            return cls()
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        store = cls(dim=payload["dim"])
        index = faiss.read_index(str(index_path))
        store._index = (
            index if isinstance(index, faiss.IndexIDMap2) else faiss.IndexIDMap2(index)
        )
        store._next_id = payload["next_id"]
        store._meta = {}
        for entry in payload["entries"]:
            entry = dict(entry)
            int_id = int(entry.pop("id"))
            store._meta[int_id] = entry
        if store._index.ntotal != len(store._meta):
            logger.warning(
                "Index/metadata mismatch: %d vectors vs %d metadata records",
                store._index.ntotal,
                len(store._meta),
            )
        return store
