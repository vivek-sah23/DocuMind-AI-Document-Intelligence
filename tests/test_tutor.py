"""Tutor chat: reply parsing, follow-up aware retrieval, grounding flags."""
from src.llm import LLMUnavailableError, format_history
from src.rag_pipeline import RAGPipeline, parse_tutor
from tests.test_rag_pipeline import hit


class TutorRetriever:
    def __init__(self, hits):
        self.hits, self.queries = hits, []

    def retrieve_deep(self, question, doc_id=None, top_k=None, max_chunks=None):
        self.queries.append(question)
        return self.hits


class TutorGenerator:
    def __init__(self, reply, rewrite="standalone query", error=None):
        self.reply, self.rewrite, self.error = reply, rewrite, error
        self.rewrites, self.tutors = [], []

    def rewrite_query(self, question, history=None):
        self.rewrites.append((question, history))
        return self.rewrite

    def generate_tutor(self, question, context, history=None):
        self.tutors.append((question, context, history))
        if self.error:
            raise self.error
        return self.reply


def test_parse_splits_followups_and_normalises_citations():
    out = parse_tutor(
        "Govern sets policy [Passage 1, Passage 2] and more \u3010Passage 3\u3011.\n"
        "---FOLLOW-UPS---\n- What is Identify?\n2. What is Protect?\n* Why Govern?\n- extra?"
    )
    assert "[Passage 1][Passage 2]" in out["answer"] and "[Passage 3]" in out["answer"]
    assert out["follow_ups"] == ["What is Identify?", "What is Protect?", "Why Govern?"]
    assert "FOLLOW" not in out["answer"]


def test_parse_flags():
    assert parse_tutor("[[NOT_IN_PDF]]\nNot covered.")["not_in_pdf"] is True
    chat = parse_tutor("[[CHAT]]\nYou're welcome!")
    assert chat["chat"] is True and chat["answer"] == "You're welcome!"


def test_first_message_skips_rewrite_and_cites():
    gen = TutorGenerator("Govern is first [Passage 2].\n---FOLLOW-UPS---\n- Next?")
    ret = TutorRetriever([hit(1, page=3), hit(2, page=8)])
    out = RAGPipeline(ret, gen).tutor_answer("What is first?", [])
    assert gen.rewrites == [] and ret.queries == ["What is first?"]
    assert out["mode"] == "tutor" and out["refused"] is False
    assert [c["page_number"] for c in out["citations"]] == [8]
    assert out["follow_ups"] == ["Next?"]


def test_follow_up_uses_rewritten_query_and_history():
    gen = TutorGenerator("More detail [Passage 1].", rewrite="Govern function details")
    ret = TutorRetriever([hit(1)])
    hist = [{"role": "user", "text": "What is Govern?"}, {"role": "assistant", "text": "It sets policy."}]
    RAGPipeline(ret, gen).tutor_answer("explain it simpler", hist)
    assert ret.queries == ["Govern function details"]
    assert gen.tutors[0][0] == "explain it simpler" and gen.tutors[0][2] == hist
    assert "Student: What is Govern?" in format_history(hist)


def test_not_in_pdf_is_flagged_without_citations():
    gen = TutorGenerator("[[NOT_IN_PDF]]\nThe document does not cover that.")
    out = RAGPipeline(TutorRetriever([]), gen).tutor_answer("capital of France?", [])
    assert out["refused"] is True and out["citations"] == []
    assert "(no passages" in gen.tutors[0][1]


def test_small_talk_has_no_citations():
    gen = TutorGenerator("[[CHAT]]\nAnytime! [Passage 1]")
    out = RAGPipeline(TutorRetriever([hit(1)]), gen).tutor_answer("thanks", [])
    assert out["chat"] is True and out["citations"] == []


def test_invented_passage_numbers_dropped():
    gen = TutorGenerator("Claim [Passage 9] and [Passage 1].")
    out = RAGPipeline(TutorRetriever([hit(1)]), gen).tutor_answer("q", [])
    assert [c["passage"] for c in out["citations"]] == [1]


def test_llm_error_falls_back_to_excerpts():
    gen = TutorGenerator("", error=LLMUnavailableError("down"))
    out = RAGPipeline(TutorRetriever([hit(1)]), gen).tutor_answer("q", [])
    assert out["degraded"] is True and out["citations"] and "[Passage 1]" in out["answer"]


def test_fallback_generator_survives_timeouts_and_tries_all_models():
    import httpx
    from src.llm import FallbackGenerator

    class Slow:
        provider = "gemini"
        def generate_tutor(self, q, c, h=None):
            raise httpx.ReadTimeout("The read operation timed out")

    class Ok:
        provider = "groq"
        def generate_tutor(self, q, c, h=None):
            return "fine"

    gen = FallbackGenerator([Slow(), Ok()])
    assert gen.generate_tutor("q", "c") == "fine" and gen.last_provider == "groq"


def test_parse_tutor_expands_citation_ranges_and_strips_boxed():
    out = parse_tutor(
        "Output is \\boxed{O = \\Big\\lfloor x \\Big\\rfloor + 1} here [Passage 1-2, 4]. Also [Passages 3 and 5]."
    )["answer"]
    assert "[Passage 1][Passage 2][Passage 4]" in out
    assert "[Passage 3][Passage 5]" in out
    assert "boxed" not in out and "Big" not in out
    assert "O = \\lfloor x \\rfloor + 1" in out


def test_parse_structured_repairs_latex_backslashes():
    from src.llm import parse_structured
    raw = '{"answer": "Use $\\frac{N}{S}$ and $\\times$ and \\lfloor x \\rfloor"}'
    got = parse_structured(raw)["answer"]
    assert "\\frac{N}{S}" in got and "\\times" in got and "\\lfloor" in got
