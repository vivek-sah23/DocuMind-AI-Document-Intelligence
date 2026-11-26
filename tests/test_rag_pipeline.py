"""Pipeline tests with a mocked generator: grounding, refusal, citation
validation and API error handling. No network calls."""
import pytest

from src.llm import LLMUnavailableError, format_context
from src.rag_pipeline import RAGPipeline


class FakeRetriever:
    def __init__(self, hits):
        self._hits = hits

    def retrieve(self, question, doc_id=None):
        return self._hits


class FakeGenerator:
    def __init__(self, response="The answer [Passage 1].", error=None):
        self.response = response
        self.error = error
        self.calls = []

    def generate_answer(self, question, formatted_context):
        self.calls.append((question, formatted_context))
        if self.error:
            raise self.error
        return self.response


def hit(n, name="doc.pdf", page=1):
    return {
        "score": 0.9 - n * 0.01,
        "doc_id": f"doc{n}",
        "doc_name": name,
        "page_number": page,
        "chunk_id": f"c{n}",
        "text": f"text of passage {n}",
    }


class TestGroundedAnswer:
    def test_answer_with_valid_citations(self):
        pipe = RAGPipeline(FakeRetriever([hit(1, page=7), hit(2, page=9)]),
                           FakeGenerator("Govern comes first [Passage 1]."))
        out = pipe.answer("What comes first?")
        assert out["answer"] == "Govern comes first [Passage 1]."
        assert out["refused"] is False
        assert len(out["citations"]) == 1
        c = out["citations"][0]
        assert c["doc_name"] == "doc.pdf"
        assert c["page_number"] == 7
        assert c["text"] == "text of passage 1"

    def test_prompt_contains_bounded_context(self):
        gen = FakeGenerator()
        pipe = RAGPipeline(FakeRetriever([hit(1), hit(2)]), gen)
        pipe.answer("question?")
        _, context = gen.calls[0]
        assert context.startswith("<context>")
        assert "[Passage 1]" in context and "[Passage 2]" in context
        # The citation boundary: no document names or pages reach the LLM.
        assert "doc.pdf" not in context


class TestCitationBoundary:
    def test_out_of_range_reference_dropped(self):
        pipe = RAGPipeline(
            FakeRetriever([hit(1)]),
            FakeGenerator("Answer [Passage 1] and [Passage 99]."),
        )
        out = pipe.answer("q")
        assert [c["passage"] for c in out["citations"]] == [1]

    def test_no_valid_reference_attaches_top_sources(self):
        pipe = RAGPipeline(
            FakeRetriever([hit(1), hit(2), hit(3), hit(4)]),
            FakeGenerator("An answer with no citation tags."),
        )
        out = pipe.answer("q")
        assert [c["passage"] for c in out["citations"]] == [1, 2, 3]

    def test_duplicate_reference_rendered_once(self):
        pipe = RAGPipeline(
            FakeRetriever([hit(1)]),
            FakeGenerator("A [Passage 1] B [Passage 1]."),
        )
        out = pipe.answer("q")
        assert len(out["citations"]) == 1


class TestRefusalAndErrors:
    def test_no_hits_refuses_without_llm_call(self):
        gen = FakeGenerator()
        pipe = RAGPipeline(FakeRetriever([]), gen)
        out = pipe.answer("unanswerable")
        assert out["refused"] is True
        assert "do not contain sufficient information" in out["answer"]
        assert gen.calls == []

    def test_empty_question_refused(self):
        pipe = RAGPipeline(FakeRetriever([hit(1)]), FakeGenerator())
        out = pipe.answer("   ")
        assert out["refused"] is True

    def test_llm_failure_raises_clean_error(self):
        pipe = RAGPipeline(
            FakeRetriever([hit(1)]),
            FakeGenerator(error=LLMUnavailableError("quota exhausted")),
        )
        with pytest.raises(LLMUnavailableError, match="quota exhausted"):
            pipe.answer("q")

    def test_refusal_text_flagged(self):
        from src.llm import REFUSAL_TEXT

        pipe = RAGPipeline(FakeRetriever([hit(1)]), FakeGenerator(REFUSAL_TEXT))
        out = pipe.answer("q")
        assert out["refused"] is True


class TestFormatContext:
    def test_numbered_blocks(self):
        ctx = format_context([hit(1), hit(2)])
        assert "[Passage 1]\ntext of passage 1" in ctx
        assert "[Passage 2]\ntext of passage 2" in ctx
