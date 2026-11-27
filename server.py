"""DocuMind web server: FastAPI backend + static SaaS frontend.

Replaces the Streamlit entrypoint from the original blueprint so the app
deploys as one service on Render with a production-grade UI. The RAG core
in src/ is unchanged and framework-free.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.config import settings
from src.document_manager import DocumentManager, DuplicateDocumentError
from src.embeddings import EmbeddingEngine
from src.llm import LLMUnavailableError, build_generator
from src.pdf_processor import PDFValidationError
from src.rag_pipeline import RAGPipeline
from src.retriever import Retriever
from src.utils import RateLimiter, get_logger
from src.vector_store import VectorStore

logger = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def build_stack() -> dict:
    settings.ensure_dirs()
    engine = EmbeddingEngine()
    store = VectorStore.load()
    manager = DocumentManager(engine, store)
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


app = FastAPI(title="DocuMind", version="2.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


chat_limiter = RateLimiter(limit=10, window_seconds=60)
upload_limiter = RateLimiter(limit=5, window_seconds=300)


def client_ip(request: Request) -> str:
    """Render terminates TLS in front of the app; the real client is the
    first hop in X-Forwarded-For."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    doc_id: str | None = None


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/app", include_in_schema=False)
def workspace():
    return FileResponse(STATIC_DIR / "app.html")


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


@app.post("/api/documents", status_code=201)
async def upload_document(request: Request, file: UploadFile = File(...)):
    if not upload_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many uploads. Try again in a few minutes.")
    data = await file.read()
    manager = app.state.stack["manager"]
    try:
        record = manager.ingest(data, file.filename or "upload.pdf")
    except DuplicateDocumentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PDFValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        logger.exception("Ingestion failed for %s", file.filename)
        raise HTTPException(status_code=500, detail="Indexing failed unexpectedly")
    return record


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: str):
    path = app.state.stack["manager"].pdf_path(doc_id)
    record = app.state.stack["manager"].get(doc_id)
    if path is None or record is None:
        raise HTTPException(status_code=404, detail="Preview not available for this document")
    safe_name = record["doc_name"].replace('"', "").replace("\n", " ")
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300",
        },
    )


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str):
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
        return app.state.stack["pipeline"].answer(
            request.question, doc_id=request.doc_id
        )
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
