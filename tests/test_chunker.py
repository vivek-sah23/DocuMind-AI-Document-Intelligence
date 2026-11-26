from src.pdf_processor import PageText
from src.text_chunker import chunk_pages, estimate_tokens, split_sentences


def pages_of(*texts: str) -> list[PageText]:
    return [
        PageText(page_number=i + 1, text=t, char_count=len(t))
        for i, t in enumerate(texts)
    ]


class TestSentenceSplit:
    def test_basic_split(self):
        s = split_sentences("First sentence. Second sentence! Third one? Yes.")
        assert s == ["First sentence.", "Second sentence!", "Third one?", "Yes."]

    def test_abbreviations_do_not_split(self):
        s = split_sentences("See Fig. 3 for details. It works, e.g. here.")
        assert s == ["See Fig. 3 for details.", "It works, e.g. here."]

    def test_decimals_and_versions_do_not_split(self):
        s = split_sentences("The score is 3.14 in v2.1 of the tool. Done now.")
        assert s == ["The score is 3.14 in v2.1 of the tool.", "Done now."]

    def test_et_al_guard(self):
        s = split_sentences("Sah et al. built the index. It held up.")
        assert s == ["Sah et al. built the index.", "It held up."]


class TestChunking:
    def test_short_text_single_chunk(self):
        chunks = chunk_pages(
            pages_of("One short sentence."), "d" * 64, "a.pdf",
            chunk_size=50, overlap=10,
        )
        assert len(chunks) == 1
        assert chunks[0].text == "One short sentence."
        assert chunks[0].page_number == 1
        assert chunks[0].doc_name == "a.pdf"

    def test_windows_respect_token_budget(self):
        text = " ".join(f"Sentence number {i} has several words in it." for i in range(12))
        chunks = chunk_pages(
            pages_of(text), "d" * 64, "a.pdf",
            chunk_size=40, overlap=8,
        )
        assert len(chunks) > 1
        for c in chunks:
            assert c.token_count <= 40 + estimate_tokens(
                "Sentence number 0 has several words in it."
            )

    def test_overlap_carries_tail_sentences(self):
        text = "Alpha one. Beta two. Gamma three. Delta four. Epsilon five. Zeta six."
        chunks = chunk_pages(
            pages_of(text), "d" * 64, "a.pdf",
            chunk_size=10, overlap=4,
        )
        assert len(chunks) >= 2
        first_tail = chunks[0].text.split(". ")[-1].rstrip(".")
        assert first_tail.split()[0] in chunks[1].text

    def test_long_sentence_hard_split_never_stalls(self):
        monster = " ".join(f"word{i}" for i in range(300))
        chunks = chunk_pages(
            pages_of(monster), "d" * 64, "a.pdf",
            chunk_size=50, overlap=10,
        )
        assert len(chunks) >= 5
        for c in chunks:
            assert c.token_count <= 50

    def test_page_metadata_inherited(self):
        chunks = chunk_pages(
            pages_of("Text on page one.", "Text on page two."),
            "d" * 64, "a.pdf", chunk_size=200, overlap=20,
        )
        assert chunks[0].page_number == 1
        assert any(c.page_number == 2 for c in chunks) or len(chunks) == 1

    def test_overlap_must_be_smaller_than_chunk(self):
        import pytest

        with pytest.raises(ValueError):
            chunk_pages(pages_of("Hi."), "d" * 64, "a.pdf",
                        chunk_size=10, overlap=10)

    def test_chunk_ids_unique_and_sequential(self):
        text = " ".join(f"Sentence {i} here." for i in range(30))
        chunks = chunk_pages(
            pages_of(text), "d" * 64, "a.pdf",
            chunk_size=25, overlap=5,
        )
        ids = [c.chunk_id for c in chunks]
        assert len(ids) == len(set(ids))
