"""PDF validation and page-aware text extraction (PyMuPDF, no wrappers)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import fitz  # PyMuPDF

from .utils import get_logger

logger = get_logger(__name__)

PDF_MAGIC = b"%PDF-"
MIN_PAGE_CHARS = 20  # below this a page is treated as scanned/empty


class PDFValidationError(ValueError):
    """Raised when an upload fails a defensive ingestion check."""


@dataclass
class PageText:
    page_number: int  # 1-indexed for human display
    text: str
    char_count: int
    needs_ocr: bool = False


@dataclass
class ExtractedDocument:
    pages: list[PageText] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return sum(p.char_count for p in self.pages)

    @property
    def scanned_pages(self) -> list[int]:
        return [p.page_number for p in self.pages if p.needs_ocr]


def validate_pdf(data: bytes, filename: str, max_size_mb: int) -> None:
    """Defensive ingestion gate: extension, size, magic bytes, structure."""
    if not filename.lower().endswith(".pdf"):
        raise PDFValidationError(f"{filename!r} is not a .pdf file")
    if len(data) == 0:
        raise PDFValidationError("Uploaded file is empty")
    if len(data) > max_size_mb * 1024 * 1024:
        raise PDFValidationError(
            f"File is {len(data) / 1024 / 1024:.1f} MB, over the {max_size_mb} MB limit"
        )
    if not data.startswith(PDF_MAGIC):
        raise PDFValidationError("File does not start with %PDF- magic bytes")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        _ = doc.page_count
        doc.close()
    except Exception as exc:  # PyMuPDF raises several concrete types
        raise PDFValidationError(f"PDF is corrupted or unreadable: {exc}") from exc


def _clean_page_text(text: str) -> str:
    """Join hyphenated line breaks and collapse redundant whitespace."""
    text = text.replace("\u00ad", "")  # soft hyphens
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # hyphenated word wrap
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_pages(data: bytes, use_ocr: bool = False) -> ExtractedDocument:
    """Extract per-page text with reading-order sorting (sort=True).

    Pages with fewer than MIN_PAGE_CHARS characters are flagged so the
    caller can report them or fall back to OCR (Milestone 9 hook).
    """
    result = ExtractedDocument()
    with fitz.open(stream=data, filetype="pdf") as doc:
        if doc.page_count == 0:
            raise PDFValidationError("PDF has no pages")
        for index in range(doc.page_count):
            page = doc.load_page(index)
            text = _clean_page_text(page.get_text("text", sort=True))
            needs_ocr = len(text) < MIN_PAGE_CHARS
            if needs_ocr and use_ocr:
                text = _ocr_page(page)
                needs_ocr = len(text) < MIN_PAGE_CHARS
            result.pages.append(
                PageText(
                    page_number=index + 1,
                    text=text,
                    char_count=len(text),
                    needs_ocr=needs_ocr,
                )
            )
    if result.total_chars == 0:
        raise PDFValidationError(
            "No extractable text found. The PDF looks fully scanned; "
            "OCR support is required to index it."
        )
    return result


def _ocr_page(page: "fitz.Page") -> str:
    """Lazy OCR fallback. Optional dependency: pytesseract + Pillow +
    a system tesseract binary. Raises PDFValidationError if unavailable."""
    try:
        import io

        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise PDFValidationError(
            "Scanned page detected but OCR dependencies "
            "(pytesseract, Pillow, tesseract) are not installed"
        ) from exc
    pix = page.get_pixmap(dpi=300)
    image = Image.open(io.BytesIO(pix.tobytes("png")))
    return _clean_page_text(pytesseract.image_to_string(image))
