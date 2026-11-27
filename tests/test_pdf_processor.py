import pytest

from src.pdf_processor import (
    PDFValidationError,
    extract_pages,
    validate_pdf,
)


class TestValidate:
    def test_valid_pdf_passes(self, one_page_pdf):
        validate_pdf(one_page_pdf, "report.pdf", max_size_mb=5)

    def test_accepts_valid_pdf_without_pdf_extension(self, one_page_pdf):
        validate_pdf(one_page_pdf, "report", max_size_mb=5)

    def test_rejects_non_pdf_content_named_pdf(self):
        with pytest.raises(PDFValidationError, match="magic"):
            validate_pdf(b"hello world", "notes.pdf", max_size_mb=5)

    def test_rejects_password_protected_pdf(self, one_page_pdf):
        import fitz

        doc = fitz.open(stream=one_page_pdf, filetype="pdf")
        locked = doc.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u"
        )
        with pytest.raises(PDFValidationError, match="password"):
            validate_pdf(locked, "locked.pdf", max_size_mb=5)

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
        with pytest.raises(PDFValidationError, match="No readable text"):
            extract_pages(blank_pdf)


class TestOcrFallback:
    @staticmethod
    def _mixed_pdf():
        import fitz

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "This page has a real text layer with plenty of words.", fontsize=11)
        doc.new_page()  # scanned-looking: no text layer
        data = doc.tobytes()
        doc.close()
        return data

    def test_ocr_fills_pages_without_text(self):
        calls = []

        def fake_ocr(png):
            calls.append(png[:4])
            return "Text recovered by the OCR step for page two."

        result = extract_pages(self._mixed_pdf(), ocr=fake_ocr)
        assert len(calls) == 1 and calls[0] == b"\x89PNG"
        assert result.pages[1].text.startswith("Text recovered")
        assert result.scanned_pages == []

    def test_ocr_budget_is_respected(self):
        import fitz

        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "Only this page has a text layer, enough words.", fontsize=11)
        for _ in range(4):
            doc.new_page()
        data = doc.tobytes()
        result = extract_pages(data, ocr=lambda png: "Recovered page text from the scan.", max_ocr_pages=2)
        assert result.scanned_pages == [4, 5]

    def test_ocr_failure_does_not_break_ingestion(self):
        def broken(png):
            raise RuntimeError("quota")

        result = extract_pages(self._mixed_pdf(), ocr=broken)
        assert result.scanned_pages == [2]

    def test_fully_scanned_without_ocr_is_rejected(self, blank_pdf):
        with pytest.raises(PDFValidationError, match="No readable text"):
            extract_pages(blank_pdf, ocr=None)
