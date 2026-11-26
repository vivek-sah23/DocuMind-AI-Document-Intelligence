"""Multi-PDF registry: ingest, SHA-256 deduplication, deletion, summaries."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .config import settings
from .embeddings import EmbeddingEngine
from .pdf_processor import PDFValidationError, extract_pages, validate_pdf
from .text_chunker import chunk_pages
from .utils import format_bytes, get_logger, sha256_bytes, utc_now_iso
from .vector_store import VectorStore

logger = get_logger(__name__)

REGISTRY_FILE = "documents.json"


class DuplicateDocumentError(RuntimeError):
    pass


class DocumentManager:
    def __init__(
        self,
        engine: EmbeddingEngine,
        store: VectorStore,
        registry_dir: Path | None = None,
    ) -> None:
        self.engine = engine
        self.store = store
        self.registry_dir = Path(registry_dir) if registry_dir else settings.metadata_dir
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._docs: dict[str, dict] = self._load_registry()

    # -- registry file ---------------------------------------------------
    def _registry_path(self) -> Path:
        return self.registry_dir / REGISTRY_FILE

    def _load_registry(self) -> dict[str, dict]:
        path = self._registry_path()
        if not path.exists():
            return {}
        records = json.loads(path.read_text(encoding="utf-8"))
        return {r["doc_id"]: r for r in records}

    def _save_registry(self) -> None:
        records = sorted(
            self._docs.values(), key=lambda r: r["uploaded_at"]
        )
        self._registry_path().write_text(
            json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    # -- public API -------------------------------------------------------
    def list_documents(self) -> list[dict]:
        return sorted(self._docs.values(), key=lambda r: r["uploaded_at"])

    def get(self, doc_id: str) -> dict | None:
        return self._docs.get(doc_id)

    def ingest(self, data: bytes, filename: str, use_ocr: bool = False) -> dict:
        """Validate, dedupe, extract, chunk, embed and index one PDF."""
        validate_pdf(data, filename, settings.max_file_size_mb)
        doc_id = sha256_bytes(data)
        with self._lock:
            if doc_id in self._docs:
                raise DuplicateDocumentError(
                    f"{filename} is already indexed as "
                    f"{self._docs[doc_id]['doc_name']}"
                )

        extracted = extract_pages(data, use_ocr=use_ocr)
        chunks = chunk_pages(
            extracted.pages,
            doc_id=doc_id,
            doc_name=filename,
            chunk_size=settings.chunk_size_tokens,
            overlap=settings.chunk_overlap_tokens,
            count_tokens=self.engine.count_tokens,
        )
        if not chunks:
            raise PDFValidationError("No indexable text found in the PDF")

        vectors = self.engine.encode([c.text for c in chunks])
        metadatas = [c.to_metadata() for c in chunks]

        with self._lock:
            self.store.add(vectors, metadatas)
            self.store.save()
            record = {
                "doc_id": doc_id,
                "doc_name": filename,
                "size": format_bytes(len(data)),
                "page_count": len(extracted.pages),
                "chunk_count": len(chunks),
                "total_chars": extracted.total_chars,
                "scanned_pages": extracted.scanned_pages,
                "uploaded_at": utc_now_iso(),
            }
            self._docs[doc_id] = record
            self._save_registry()
        logger.info(
            "Ingested %s: %d pages, %d chunks", filename, len(extracted.pages), len(chunks)
        )
        return record

    def delete(self, doc_id: str) -> bool:
        with self._lock:
            if doc_id not in self._docs:
                return False
            removed = self.store.remove_doc(doc_id)
            self.store.save()
            del self._docs[doc_id]
            self._save_registry()
        logger.info("Deleted doc %s (%d vectors)", doc_id[:12], removed)
        return True

    def summary(self, doc_id: str) -> dict | None:
        doc = self._docs.get(doc_id)
        if doc is None:
            return None
        return {
            **doc,
            "avg_chunk_tokens": round(
                sum(
                    m["token_count"]
                    for m in self.store._meta.values()
                    if m["doc_id"] == doc_id
                )
                / max(doc["chunk_count"], 1),
                1,
            ),
        }
