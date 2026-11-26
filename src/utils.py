"""Shared helpers: logging, hashing, formatting."""
from __future__ import annotations

import hashlib
import logging
import sys
from datetime import datetime, timezone


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def sha256_bytes(data: bytes) -> str:
    """Deterministic document id from content, used for deduplication."""
    return hashlib.sha256(data).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_bytes(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def redact(text: str, secret: str | None = None) -> str:
    """Strip the configured secret and common key shapes from any text
    before it is logged or returned to a client."""
    import re

    out = str(text)
    if secret:
        out = out.replace(secret, "[redacted]")
    return re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "[redacted]", out)


class RateLimiter:
    """Sliding-window per-key limiter (in memory, single worker)."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        import threading
        from collections import defaultdict, deque

        self.limit = limit
        self.window = window_seconds
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        import time

        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True
