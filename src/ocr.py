"""OCR fallback for scanned pages, using Gemini vision (free tier).

Only pages with no extractable text are sent. The PNG never touches disk and
the API key is redacted from any error that reaches the logs.
"""
from __future__ import annotations

import time

from .config import settings
from .utils import get_logger, redact

logger = get_logger(__name__)

PROMPT = (
    "Transcribe all readable text in this page image, in reading order. "
    "Output only the text, no commentary. If there is no text, output nothing."
)


class GeminiOCR:
    def __init__(self, api_key: str | None = None, model_name: str | None = None):
        self.api_key = api_key if api_key is not None else settings.gemini_api_key
        self.model_name = model_name or settings.gemini_model
        self._client = None

    def __call__(self, png: bytes) -> str:
        from google import genai
        from google.genai import types

        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        delay = 1.5
        for attempt in range(1, 4):
            try:
                resp = self._client.models.generate_content(
                    model=self.model_name,
                    contents=[types.Part.from_bytes(data=png, mime_type="image/png"), PROMPT],
                )
                return (resp.text or "").strip()
            except Exception as exc:  # rate limit, transient 5xx, safety block
                logger.warning("OCR attempt %d failed: %s", attempt, redact(exc, self.api_key))
                if attempt == 3:
                    return ""
                time.sleep(delay)
                delay *= 2
        return ""
