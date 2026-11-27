"""Orchestrates retrieval, prompt assembly and citation binding.

The citation boundary: the LLM only ever sees [Passage N] labels. This
module maps validated N values back to the (doc_name, page_number,
chunk_id, text) records from the metadata store. A hallucinated or
out-of-range tag is dropped, never rendered.
"""
from __future__ import annotations

import re
import time

from .llm import REFUSAL_TEXT, LLMUnavailableError, format_context, parse_structured
from .retriever import Retriever
from .utils import get_logger, redact

logger = get_logger(__name__)

_PASSAGE_REF = re.compile(r"\[Passage\s+(\d+)\]", re.IGNORECASE)


class RAGPipeline:
    def __init__(self, retriever: Retriever, generator) -> None:
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

        parsed = parse_structured(raw_answer)
        explanation, key_points, memory_tip = "", [], ""
        if parsed is not None:
            if parsed.get("sufficient") is False or not str(parsed.get("answer", "")).strip():
                answer_text = REFUSAL_TEXT
            else:
                answer_text = str(parsed["answer"]).strip()
                explanation = str(parsed.get("explanation") or "").strip()
                memory_tip = str(parsed.get("memory_tip") or "").strip()
                raw_points = parsed.get("key_points") or []
                if isinstance(raw_points, list):
                    key_points = [str(p).strip() for p in raw_points if str(p).strip()][:6]
        else:
            answer_text = raw_answer.strip()

        refused = answer_text.startswith(
            "The uploaded document(s) do not contain sufficient information"
        )
        if refused:
            return {
                "answer": REFUSAL_TEXT,
                "explanation": "",
                "key_points": [],
                "memory_tip": "",
                "citations": [],
                "refused": True,
                "provider": getattr(self.generator, "last_provider", None),
                "latency_ms": self._ms(started),
            }
        all_text = " ".join([answer_text, explanation, memory_tip, *key_points])
        citations = self._resolve_citations(all_text, hits)
        return {
            "answer": answer_text,
            "explanation": explanation,
            "key_points": key_points,
            "memory_tip": memory_tip,
            "citations": citations,
            "refused": False,
            "provider": getattr(self.generator, "last_provider", None),
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
                    "doc_id": hit["doc_id"],
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
