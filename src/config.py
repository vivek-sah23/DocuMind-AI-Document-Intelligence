"""Central configuration for DocuMind.

All environment variables are loaded and validated here, once.
Every other module imports `settings` and never touches os.environ.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}")


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    # LLM
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    # Verified GA endpoint as of Nov 2026 (aistudio.google.com/docs/models).
    # Override in .env, e.g. gemini-4-hydrogen for higher quality.
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    # Groq (OpenAI-compatible, very fast inference). Used first when a key is
    # set; Gemini stays as the automatic fallback.
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    llm_temperature: float = 0.1
    llm_max_output_tokens: int = 1024
    llm_max_retries: int = 3

    # Embeddings. all-MiniLM-L6-v2 truncates input at 256 word-pieces,
    # so chunks are sized well below that using the model's own tokenizer.
    embedding_model: str = os.getenv(
        "EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
    )
    embedding_dim: int = 384

    # Chunking (correction: 500/100 loses the second half of every chunk
    # to the 256-token truncation of MiniLM; 200/40 fits the window).
    chunk_size_tokens: int = field(
        default_factory=lambda: _int_env("CHUNK_SIZE_TOKENS", 200)
    )
    chunk_overlap_tokens: int = field(
        default_factory=lambda: _int_env("CHUNK_OVERLAP_TOKENS", 40)
    )

    # Retrieval
    top_k: int = field(default_factory=lambda: _int_env("TOP_K", 5))
    # Starting point only; tune on your own in-scope and out-of-scope
    # questions and report recall@k before changing it in production.
    min_similarity_score: float = field(
        default_factory=lambda: _float_env("MIN_SIMILARITY_SCORE", 0.25)
    )

    # Ingestion
    max_file_size_mb: int = field(
        default_factory=lambda: _int_env("MAX_FILE_SIZE_MB", 50)
    )

    # Storage
    index_dir: Path = BASE_DIR / "storage" / "indexes"
    metadata_dir: Path = BASE_DIR / "storage" / "metadata"
    pdf_dir: Path = BASE_DIR / "storage" / "pdfs"
    sample_dir: Path = BASE_DIR / "data" / "sample_pdfs"

    def ensure_dirs(self) -> None:
        for d in (self.index_dir, self.metadata_dir, self.pdf_dir, self.sample_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def groq_configured(self) -> bool:
        return bool(self.groq_api_key)

    @property
    def llm_configured(self) -> bool:
        return self.groq_configured or self.gemini_configured

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)


settings = Settings()
