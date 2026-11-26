"""Orchestrates retrieval, prompt assembly and citation binding.

The citation boundary: the LLM only ever sees [Passage N] labels. This
module maps validated N values back to the (doc_name, page_number,
chunk_id, text) records from the metadata store. A hallucinated or
out-of-range tag is dropped, never rendered.
"""
from __future__ import annotations

import re
import time

from .llm import REFUSAL_TEXT, GeminiGenerator, LLMUnavailableError, format_context
from .retriever import Retriever
from .utils import get_logger, redact

logger = get_logger(__name__)

_PASSAGE_REF = re.compile(r"\[Passage\s+(\d+)\]", re.IGNORECASE)


class RAGPipeline:
    def __init__(self, retriever: Retriever, generator: GeminiGenerator) -> None:
        self.retriever = retriever
        self.generator = generator

    def answer(self, question: str, doc_id: str | None = None) -> dict:
        started = time.perf_counter()
        question = question.strip()
        if not question:
            return {
                "answer": "Ask a question about your uploaded documents.",
                "citations": [],
                "refused": True,
                "latency_ms": 0,
            }

        hits = self.retriever.retrieve(question, doc_id=doc_id)
        if not hits:
            return {
                "answer": REFUSAL_TEXT,
                "citations": [],
                "refused": True,
                "latency_ms": self._ms(started),
            }

        context = format_context(hits)
        try:
            raw_answer = self.generator.generate_answer(question, context)
        except LLMUnavailableError as exc:
            raise
        except Exception as exc:  # defensive: never leak a stack to the UI
            logger.error("Generation failed: %s", redact(exc))
            raise LLMUnavailableError("Answer generation failed") from None

        citations = self._resolve_citations(raw_answer, hits)
        refused = raw_answer.strip().startswith(
            "The uploaded document(s) do not contain sufficient information"
        )
        return {
            "answer": raw_answer,
            "citations": citations,
            "refused": refused,
            "latency_ms": self._ms(started),
        }

    @staticmethod
    def _resolve_citations(answer: str, hits: list[dict]) -> list[dict]:
        """Keep only passage references that exist in the retrieved set."""
        refs = []
        for match in _PASSAGE_REF.finditer(answer):
            n = int(match.group(1))
            if 1 <= n <= len(hits) and n not in refs:
                refs.append(n)
        if not refs:
            # Model answered without citing; attach the top sources so the
            # UI still shows verifiable provenance.
            refs = list(range(1, min(3, len(hits)) + 1))
        citations = []
        for n in refs:
            hit = hits[n - 1]
            citations.append(
                {
                    "passage": n,
                    "doc_name": hit["doc_name"],
                    "page_number": hit["page_number"],
                    "chunk_id": hit["chunk_id"],
                    "score": round(hit["score"], 4),
                    "text": hit["text"],
                }
            )
        return citations

    @staticmethod
    def _ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)
