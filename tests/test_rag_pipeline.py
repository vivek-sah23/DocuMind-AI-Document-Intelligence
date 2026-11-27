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


class TestStructuredAnswer:
    JSON = (
        '{"sufficient": true, "answer": "Six functions [Passage 1].",'
        ' "explanation": "They form the core [Passage 2].",'
        ' "key_points": ["Govern is new [Passage 1]", "Recover"],'
        ' "memory_tip": "GIP-DRR"}'
    )

    def test_structured_fields_returned_with_citations(self):
        pipe = RAGPipeline(FakeRetriever([hit(1, page=3), hit(2, page=8)]),
                           FakeGenerator(self.JSON))
        out = pipe.answer("q")
        assert out["answer"] == "Six functions [Passage 1]."
        assert out["explanation"].startswith("They form")
        assert out["key_points"] == ["Govern is new [Passage 1]", "Recover"]
        assert out["memory_tip"] == "GIP-DRR"
        assert [c["page_number"] for c in out["citations"]] == [3, 8]

    def test_insufficient_json_is_refusal(self):
        pipe = RAGPipeline(FakeRetriever([hit(1)]), FakeGenerator('{"sufficient": false}'))
        out = pipe.answer("q")
        assert out["refused"] is True
        assert out["citations"] == []
        assert out["memory_tip"] == ""

    def test_fenced_json_is_parsed(self):
        pipe = RAGPipeline(FakeRetriever([hit(1)]),
                           FakeGenerator("```json\n" + self.JSON + "\n```"))
        assert pipe.answer("q")["memory_tip"] == "GIP-DRR"

    def test_plain_text_still_works(self):
        pipe = RAGPipeline(FakeRetriever([hit(1)]), FakeGenerator("Plain [Passage 1]."))
        out = pipe.answer("q")
        assert out["answer"] == "Plain [Passage 1]."
        assert out["memory_tip"] == ""


class TestGroq:
    def _gen(self, handler):
        import httpx
        from src.llm import GroqGenerator

        return GroqGenerator(api_key="gsk_" + "a" * 30, model_name="m",
                             transport=httpx.MockTransport(handler))

    def test_success_sends_bearer_and_json_mode(self):
        import httpx, json

        seen = {}

        def handler(request):
            seen["auth"] = request.headers["authorization"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer": "ok"}'}}]})

        out = self._gen(handler).generate_answer("q", "<context></context>")
        assert out == '{"answer": "ok"}'
        assert seen["auth"].startswith("Bearer gsk_")
        assert seen["body"]["response_format"] == {"type": "json_object"}
        assert "reasoning_effort" not in seen["body"]  # model "m" is not gpt-oss

    def test_gpt_oss_uses_low_reasoning(self):
        import httpx, json
        from src.llm import GroqGenerator

        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

        GroqGenerator(api_key="k", model_name="openai/gpt-oss-120b",
                      transport=httpx.MockTransport(handler)).generate_answer("q", "c")
        assert seen["body"]["reasoning_effort"] == "low"

    def test_client_error_raises_without_key(self):
        import httpx
        from src.llm import LLMUnavailableError

        gen = self._gen(lambda r: httpx.Response(401, json={"error": "bad"}))
        with pytest.raises(LLMUnavailableError) as exc:
            gen.generate_answer("q", "c")
        assert "gsk_" not in str(exc.value)

    def test_fallback_uses_next_provider(self):
        from src.llm import FallbackGenerator

        class Down:
            provider = "groq"
            def generate_answer(self, q, c):
                raise LLMUnavailableError("down")

        class Up:
            provider = "gemini"
            def generate_answer(self, q, c):
                return "fine"

        fb = FallbackGenerator([Down(), Up()])
        assert fb.generate_answer("q", "c") == "fine"
        assert fb.last_provider == "gemini"

    def test_fallback_with_no_keys(self):
        from src.llm import FallbackGenerator

        with pytest.raises(LLMUnavailableError, match="GROQ_API_KEY"):
            FallbackGenerator([]).generate_answer("q", "c")


class DeepRetriever(FakeRetriever):
    def retrieve_deep(self, question, doc_id=None):
        return self._hits


class DeepGenerator:
    last_provider = "fake"

    def __init__(self, response):
        self.response = response
        self.calls = []

    def generate_deep(self, question, formatted_context):
        self.calls.append((question, formatted_context))
        return self.response


class TestDeepDive:
    GOOD = (
        '{"sufficient": true, "title": "Govern", "overview": "Overview [Passage 2].",'
        ' "sections": [{"heading": "Start", "body": "Body [Passage 1] and [Passage 9]."},'
        ' {"heading": "", "body": ""}], "key_points": ["a [Passage 3]", ""],'
        ' "memory_tip": "tip", "gaps": "Nothing on cost."}'
    )

    def test_structured_deep_answer_and_citation_validation(self):
        hits = [hit(1, page=9), hit(2, page=3), hit(3, page=5)]
        pipe = RAGPipeline(DeepRetriever(hits), DeepGenerator(self.GOOD))
        out = pipe.deep_answer("Explain govern")
        assert out["mode"] == "deep" and out["refused"] is False
        assert len(out["sections"]) == 1  # empty section dropped
        assert out["key_points"] == ["a [Passage 3]"]
        assert out["gaps"] == "Nothing on cost."
        # Passage 9 does not exist: dropped. Pages sorted ascending.
        assert [c["passage"] for c in out["citations"]] == [2, 3, 1]
        assert out["pages"] == [3, 5, 9]

    def test_no_hits_refuses_without_calling_model(self):
        gen = DeepGenerator(self.GOOD)
        out = RAGPipeline(DeepRetriever([]), gen).deep_answer("x")
        assert out["refused"] is True and gen.calls == []

    def test_insufficient_or_bad_json_refuses(self):
        for raw in ('{"sufficient": false}', "not json"):
            out = RAGPipeline(DeepRetriever([hit(1)]), DeepGenerator(raw)).deep_answer("x")
            assert out["refused"] is True

    def test_llm_error_propagates(self):
        class Boom:
            def generate_deep(self, q, c):
                raise LLMUnavailableError("down")
        with pytest.raises(LLMUnavailableError):
            RAGPipeline(DeepRetriever([hit(1)]), Boom()).deep_answer("x")
