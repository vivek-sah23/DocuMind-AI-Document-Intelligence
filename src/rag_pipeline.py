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
from .config import settings
from .retriever import Retriever
from .utils import get_logger, redact

logger = get_logger(__name__)

_PASSAGE_REF = re.compile(r"\[Passage\s+(\d+)\]", re.IGNORECASE)


_FOLLOW_MARK = re.compile(r"-{2,}\s*FOLLOW-?UPS?\s*-{2,}", re.IGNORECASE)


_ODD_CITE = re.compile(r"[\u3010\[(]\s*Passages?\s+(\d+)\s*[\u3011\])]", re.IGNORECASE)


_MULTI_CITE = re.compile(r"\[\s*Passages?\s+(\d+(?:\s*(?:,|and|&)\s*(?:Passages?\s+)?\d+)+)\s*\]", re.IGNORECASE)


def _split_multi(m: re.Match) -> str:
    nums = re.findall(r"\d+", m.group(1))
    return "".join(f"[Passage {n}]" for n in nums)


def parse_tutor(raw: str) -> dict:
    """Split the tutor reply into answer text, control tokens and follow-ups."""
    text = (raw or "").strip()
    text = _ODD_CITE.sub(lambda m: f"[Passage {m.group(1)}]", text)
    text = _MULTI_CITE.sub(_split_multi, text)
    not_in_pdf = "[[NOT_IN_PDF]]" in text
    chat = "[[CHAT]]" in text
    text = text.replace("[[NOT_IN_PDF]]", "").replace("[[CHAT]]", "").strip()
    follow_ups: list[str] = []
    parts = _FOLLOW_MARK.split(text, maxsplit=1)
    text = parts[0].strip()
    if len(parts) == 2:
        for line in parts[1].splitlines():
            q = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s*", "", line).strip()
            if q and len(q) <= 200:
                follow_ups.append(q)
    return {"answer": text, "follow_ups": follow_ups[:3],
            "not_in_pdf": not_in_pdf, "chat": chat}


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

    def deep_answer(self, question: str, doc_id: str | None = None) -> dict:
        """In-depth explanation of a topic gathered from across the document."""
        started = time.perf_counter()
        question = question.strip()
        refused = {
            "mode": "deep",
            "answer": REFUSAL_TEXT,
            "citations": [],
            "refused": True,
        }
        if not question:
            return {**refused, "answer": "Ask a question about your uploaded documents.",
                    "latency_ms": 0}
        hits = self.retriever.retrieve_deep(question, doc_id=doc_id)
        if not hits:
            return {**refused, "latency_ms": self._ms(started)}
        try:
            raw = self.generator.generate_deep(question, format_context(hits))
        except LLMUnavailableError:
            raise
        except Exception as exc:
            logger.error("Deep generation failed: %s", redact(exc))
            raise LLMUnavailableError("Answer generation failed") from None
        parsed = parse_structured(raw)
        provider = getattr(self.generator, "last_provider", None)
        if parsed is None or parsed.get("sufficient") is False:
            return {**refused, "provider": provider, "latency_ms": self._ms(started)}

        def clean(v) -> str:
            return str(v or "").strip()

        sections = []
        for sec in parsed.get("sections") or []:
            if isinstance(sec, dict) and clean(sec.get("body")):
                sections.append({"heading": clean(sec.get("heading")), "body": clean(sec.get("body"))})
        overview = clean(parsed.get("overview"))
        if not overview and not sections:
            return {**refused, "provider": provider, "latency_ms": self._ms(started)}
        points = parsed.get("key_points") or []
        key_points = [clean(p) for p in points if clean(p)][:8] if isinstance(points, list) else []
        memory_tip, gaps = clean(parsed.get("memory_tip")), clean(parsed.get("gaps"))
        all_text = " ".join([overview, memory_tip, gaps, *key_points,
                             *[x["body"] for x in sections]])
        citations = self._resolve_citations(all_text, hits, fallback=False)
        citations.sort(key=lambda c: (c["doc_name"], c["page_number"], c["passage"]))
        return {
            "mode": "deep",
            "title": clean(parsed.get("title")) or question[:80],
            "answer": overview,
            "sections": sections,
            "key_points": key_points,
            "memory_tip": memory_tip,
            "gaps": gaps,
            "citations": citations,
            "refused": False,
            "pages": sorted({c["page_number"] for c in citations}),
            "provider": provider,
            "latency_ms": self._ms(started),
        }

    def tutor_answer(self, question: str, history: list[dict] | None = None,
                     doc_id: str | None = None) -> dict:
        """Conversational tutor reply: remembers earlier turns, retrieves
        with a follow-up-aware query, answers grounded in the document."""
        started = time.perf_counter()
        question = question.strip()
        base = {"mode": "tutor", "citations": [], "follow_ups": [], "refused": False}
        if not question:
            return {**base, "answer": "Ask a question about your uploaded documents.",
                    "refused": True, "latency_ms": 0}
        history = [h for h in (history or []) if h.get("text")][-settings.tutor_history_turns:]

        query = question
        if history:
            try:
                rewritten = self.generator.rewrite_query(question, history).strip().strip('"')
                if 3 <= len(rewritten) <= 400:
                    query = rewritten
            except LLMUnavailableError:
                raise
            except Exception as exc:
                logger.warning("Query rewrite failed: %s", redact(exc))
        kw = {"top_k": settings.tutor_top_k, "max_chunks": settings.tutor_max_chunks}
        hits = self.retriever.retrieve_deep(query, doc_id=doc_id, **kw)
        if not hits and query != question:
            hits = self.retriever.retrieve_deep(question, doc_id=doc_id, **kw)
        context = (format_context(hits) if hits
                   else "<context>\n(no passages from the document matched this message)\n</context>")
        try:
            raw = self.generator.generate_tutor(question, context, history)
        except LLMUnavailableError:
            raise
        except Exception as exc:
            logger.error("Tutor generation failed: %s", redact(exc))
            raise LLMUnavailableError("Answer generation failed") from None
        provider = getattr(self.generator, "last_provider", None)
        parsed = parse_tutor(raw)
        if not parsed["answer"]:
            return {**base, "answer": "I could not put an answer together. Please try rephrasing.",
                    "refused": True, "provider": provider, "latency_ms": self._ms(started)}
        citations = []
        if not parsed["chat"] and hits:
            citations = self._resolve_citations(parsed["answer"], hits, fallback=False)
            citations.sort(key=lambda c: (c["doc_name"], c["page_number"], c["passage"]))
        return {
            "mode": "tutor",
            "answer": parsed["answer"],
            "follow_ups": parsed["follow_ups"],
            "citations": citations,
            "pages": sorted({c["page_number"] for c in citations}),
            "refused": parsed["not_in_pdf"],
            "chat": parsed["chat"],
            "provider": provider,
            "latency_ms": self._ms(started),
        }

    @staticmethod
    def _resolve_citations(answer: str, hits: list[dict], fallback: bool = True) -> list[dict]:
        """Keep only passage references that exist in the retrieved set."""
        refs = []
        for match in _PASSAGE_REF.finditer(answer):
            n = int(match.group(1))
            if 1 <= n <= len(hits) and n not in refs:
                refs.append(n)
        if not refs and fallback:
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
