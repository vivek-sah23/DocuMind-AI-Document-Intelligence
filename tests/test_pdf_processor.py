import pytest

from src.pdf_processor import (
    PDFValidationError,
    extract_pages,
    validate_pdf,
)


class TestValidate:
    def test_valid_pdf_passes(self, one_page_pdf):
        validate_pdf(one_page_pdf, "report.pdf", max_size_mb=5)

    def test_rejects_non_pdf_extension(self, one_page_pdf):
        with pytest.raises(PDFValidationError, match="not a .pdf"):
            validate_pdf(one_page_pdf, "report.txt", max_size_mb=5)

    def test_rejects_empty_file(self):
        with pytest.raises(PDFValidationError, match="empty"):
            validate_pdf(b"", "report.pdf", max_size_mb=5)

    def test_rejects_oversize(self, one_page_pdf):
        with pytest.raises(PDFValidationError, match="over the"):
            validate_pdf(one_page_pdf, "report.pdf", max_size_mb=0)

    def test_rejects_bad_magic_bytes(self):
        with pytest.raises(PDFValidationError, match="magic"):
            validate_pdf(b"NOTAPDF" + b"x" * 100, "report.pdf", max_size_mb=5)

    def test_rejects_corrupted_pdf(self):
        corrupted = b"%PDF-1.4\n" + b"garbage" * 50
        with pytest.raises(PDFValidationError, match="corrupted"):
            validate_pdf(corrupted, "report.pdf", max_size_mb=5)


class TestExtract:
    def test_extracts_all_pages_in_order(self, two_page_pdf):
        result = extract_pages(two_page_pdf)
        assert len(result.pages) == 2
        assert result.pages[0].page_number == 1
        assert result.pages[1].page_number == 2
        assert "retrieval pipeline" in result.pages[0].text
        assert "Cosine similarity" in result.pages[1].text

    def test_char_counts_recorded(self, one_page_pdf):
        result = extract_pages(one_page_pdf)
        assert result.pages[0].char_count == len(result.pages[0].text)
        assert result.total_chars > 0

    def test_blank_page_flagged_for_ocr(self, blank_pdf):
        with pytest.raises(PDFValidationError, match="No extractable text"):
            extract_pages(blank_pdf)
