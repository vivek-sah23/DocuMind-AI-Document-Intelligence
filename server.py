"""DocuMind web server: FastAPI backend + static SaaS frontend.

Replaces the Streamlit entrypoint from the original blueprint so the app
deploys as one service on Render with a production-grade UI. The RAG core
in src/ is unchanged and framework-free.
"""
from __future__ import annotations

from urllib.parse import quote
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.config import settings
from src.document_manager import DocumentManager, DuplicateDocumentError
from src.embeddings import EmbeddingEngine
from src.llm import LLMUnavailableError, build_generator
from src.ocr import GeminiOCR
from src.pdf_processor import PDFValidationError, validate_pdf
from src.rag_pipeline import RAGPipeline
from src.retriever import Retriever
from src.utils import RateLimiter, get_logger, sha256_bytes
from src.vector_store import VectorStore

logger = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def build_stack() -> dict:
    settings.ensure_dirs()
    engine = EmbeddingEngine()
    store = VectorStore.load()
    manager = DocumentManager(
        engine, store, ocr=GeminiOCR() if settings.gemini_configured else None
    )
    retriever = Retriever(engine, store)
    pipeline = RAGPipeline(retriever, build_generator())
    return {
        "engine": engine,
        "store": store,
        "manager": manager,
        "pipeline": pipeline,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.stack = build_stack()
    logger.info("DocuMind ready: %d vectors indexed", app.state.stack["store"].size)
    yield


app = FastAPI(title="DocuMind", version="3.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _asset_version() -> str:
    """Content hash of every static file; changes whenever any asset changes."""
    h = hashlib.sha256()
    for f in sorted(STATIC_DIR.rglob("*")):
        if f.is_file():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


ASSET_VERSION = _asset_version()


@app.middleware("http")
async def cache_control(request: Request, call_next):
    """Browsers must never mix a new page with a stale stylesheet.

    HTML is served no-cache so it is always revalidated. Static assets are
    immutable only when the URL carries the current content hash (?v=...);
    anything unversioned is revalidated too."""
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/app"):
        response.headers["Cache-Control"] = "no-cache"
    elif path.startswith("/static/"):
        if request.query_params.get("v") == ASSET_VERSION:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
    return response


def _page(name: str) -> HTMLResponse:
    html = (STATIC_DIR / name).read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__v__", ASSET_VERSION))


chat_limiter = RateLimiter(limit=10, window_seconds=60)
deep_limiter = RateLimiter(limit=4, window_seconds=60)
upload_limiter = RateLimiter(limit=5, window_seconds=300)


def client_ip(request: Request) -> str:
    """Render terminates TLS in front of the app; the real client is the
    first hop in X-Forwarded-For."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class HistoryTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    text: str = Field(max_length=3000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    doc_id: str | None = None
    mode: str = Field(default="tutor", pattern="^(tutor|quick|deep)$")
    history: list[HistoryTurn] = Field(default_factory=list, max_length=12)


@app.get("/", include_in_schema=False)
def index():
    return _page("index.html")


@app.get("/app", include_in_schema=False)
def workspace():
    return _page("app.html")


@app.get("/api/health")
def health():
    stack = app.state.stack
    docs = stack["manager"].list_documents()
    return {
        "status": "ok",
        "embedding_model": stack["engine"].model_name,
        "gemini_model": settings.gemini_model,
        "gemini_configured": settings.gemini_configured,
        "groq_configured": settings.groq_configured,
        "groq_model": settings.groq_model if settings.groq_configured else None,
        "llm_configured": settings.llm_configured,
        "documents": len(docs),
        "vectors": stack["store"].size,
    }


@app.get("/api/documents")
def list_documents():
    return {"documents": app.state.stack["manager"].list_documents()}


# Indexing runs off the request thread so the UI can show the PDF at once and
# poll for progress. One worker keeps memory flat on the 512 MB free plan.
_indexer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="indexer")
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _clean_name(raw: str | None) -> str:
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    return name[:180] or "document.pdf"


def _run_index_job(doc_id: str, data: bytes, name: str) -> None:
    try:
        app.state.stack["manager"].ingest(data, name)
        status = {"status": "ready"}
    except DuplicateDocumentError:
        status = {"status": "ready"}
    except PDFValidationError as exc:
        status = {"status": "failed", "error": str(exc)}
    except Exception:
        logger.exception("Ingestion failed for %s", name)
        status = {"status": "failed", "error": "Indexing failed unexpectedly. Please try again."}
    with _jobs_lock:
        _jobs[doc_id].update(status, finished=time.time())


def _job_view(doc_id: str) -> dict | None:
    manager = app.state.stack["manager"]
    record = manager.get(doc_id)
    if record is not None:
        return {"doc_id": doc_id, "status": "ready", "document": record}
    with _jobs_lock:
        job = _jobs.get(doc_id)
        if job is None:
            return None
        return {"doc_id": doc_id, "status": job["status"], "doc_name": job["name"], "error": job.get("error")}


@app.post("/api/documents", status_code=202)
async def upload_document(request: Request, file: UploadFile = File(...)):
    if not upload_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many uploads. Try again in a few minutes.")
    data = await file.read()
    name = _clean_name(file.filename)
    try:
        validate_pdf(data, name, settings.max_file_size_mb)
    except PDFValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    doc_id = sha256_bytes(data)
    manager = app.state.stack["manager"]
    existing = manager.get(doc_id)
    if existing is not None:
        return JSONResponse(
            {"doc_id": doc_id, "status": "ready", "duplicate": True, "document": existing},
            status_code=200,
        )
    with _jobs_lock:
        job = _jobs.get(doc_id)
        if job is None or job["status"] == "failed":
            _jobs[doc_id] = {"status": "indexing", "name": name}
            _indexer.submit(_run_index_job, doc_id, data, name)
    return {"doc_id": doc_id, "status": "indexing", "doc_name": name}


@app.get("/api/documents/{doc_id}/status")
def document_status(doc_id: str):
    view = _job_view(doc_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Unknown document")
    return view


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: str):
    path = app.state.stack["manager"].pdf_path(doc_id)
    record = app.state.stack["manager"].get(doc_id)
    if path is None or record is None:
        raise HTTPException(status_code=404, detail="Preview not available for this document")
    # Header values must be latin-1: names with dashes, accents or non-Latin
    # scripts need an ASCII fallback plus an RFC 5987 filename* form, or the
    # response fails with a 500 and the preview cannot load.
    raw_name = record["doc_name"].replace('"', "").replace("\n", " ").replace("\r", " ")
    ascii_name = raw_name.encode("ascii", "ignore").decode().strip() or "document.pdf"
    disposition = f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(raw_name, safe='')}"
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={
            "Content-Disposition": disposition,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300",
        },
    )


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str):
    with _jobs_lock:
        _jobs.pop(doc_id, None)
    if not app.state.stack["manager"].delete(doc_id):
        raise HTTPException(status_code=404, detail="Document not found")
    return {"deleted": doc_id}


@app.post("/api/chat")
def chat(request: ChatRequest, http: Request):
    if not chat_limiter.allow(client_ip(http)):
        raise HTTPException(
            status_code=429,
            detail="Too many questions. Please wait a minute and try again.",
        )
    if request.mode == "deep" and not deep_limiter.allow(client_ip(http)):
        raise HTTPException(
            status_code=429,
            detail="Deep dives are heavier. Please wait a minute and try again.",
        )
    if not settings.llm_configured:
        raise HTTPException(
            status_code=503,
            detail="No LLM API key is configured on the server yet (GROQ_API_KEY or GEMINI_API_KEY).",
        )
    if app.state.stack["store"].size == 0:
        raise HTTPException(
            status_code=400,
            detail="Upload a PDF first so there is something to search.",
        )
    try:
        pipeline = app.state.stack["pipeline"]
        if request.mode == "tutor":
            return pipeline.tutor_answer(
                request.question,
                [h.model_dump() for h in request.history],
                doc_id=request.doc_id,
            )
        if request.mode == "deep":
            return pipeline.deep_answer(request.question, doc_id=request.doc_id)
        return pipeline.answer(request.question, doc_id=request.doc_id)
    except LLMUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        workers=1,
    )
