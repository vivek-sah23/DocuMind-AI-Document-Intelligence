"""API tests: health, upload validation and chat guards. The upload
success path runs against a stack bound to a tmp dir."""
import pytest
from fastapi.testclient import TestClient

from server import app
from src.document_manager import DocumentManager
from src.rag_pipeline import RAGPipeline
from src.vector_store import VectorStore
from tests.test_document_manager import FakeEngine


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["embedding_model"].endswith("all-MiniLM-L6-v2")
    assert body["gemini_configured"] is False  # no key in test env


def test_frontend_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "DocuMind" in r.text


def test_upload_rejects_non_pdf(client):
    r = client.post(
        "/api/documents",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 422


def test_upload_rejects_garbage_pdf(client):
    r = client.post(
        "/api/documents",
        files={"file": ("bad.pdf", b"%PDF- broken", "application/pdf")},
    )
    assert r.status_code == 422


def test_chat_requires_gemini_key(client):
    r = client.post("/api/chat", json={"question": "anything"})
    assert r.status_code == 503
    assert "GEMINI_API_KEY" in r.json()["detail"]


def test_full_flow_against_tmp_stack(client, tmp_path, two_page_pdf):
    # Swap in a stack whose registry and index live in a tmp dir, with a
    # fake LLM so no Gemini key is needed.
    store = VectorStore(dim=384)
    manager = DocumentManager(FakeEngine(), store, registry_dir=tmp_path)
    app.state.stack["manager"] = manager
    app.state.stack["store"] = store

    class FakePipeline:
        def answer(self, question, doc_id=None):
            return {
                "answer": "mocked [Passage 1]",
                "citations": [],
                "refused": False,
                "latency_ms": 1,
            }

    app.state.stack["pipeline"] = FakePipeline()

    import src.config as config

    object.__setattr__(config.settings, "gemini_api_key", "test-key")
    try:
        r = client.post(
            "/api/documents",
            files={"file": ("notes.pdf", two_page_pdf, "application/pdf")},
        )
        assert r.status_code == 201, r.text
        doc_id = r.json()["doc_id"]

        r = client.get("/api/documents")
        assert any(d["doc_id"] == doc_id for d in r.json()["documents"])

        r = client.post("/api/chat", json={"question": "what is inside?"})
        assert r.status_code == 200
        assert r.json()["answer"].startswith("mocked")

        r = client.delete(f"/api/documents/{doc_id}")
        assert r.status_code == 200
    finally:
        object.__setattr__(config.settings, "gemini_api_key", "")


def test_chat_validation(client):
    r = client.post("/api/chat", json={"question": ""})
    assert r.status_code == 422


def test_rate_limiter_blocks_after_limit():
    from src.utils import RateLimiter

    rl = RateLimiter(limit=3, window_seconds=60)
    assert [rl.allow("a") for _ in range(4)] == [True, True, True, False]
    assert rl.allow("b") is True


def test_redact_removes_secret():
    from src.utils import redact

    assert "SECRET123" not in redact("boom SECRET123 trace", "SECRET123")
    assert "AIza" not in redact("key=AIza" + "x" * 30)
