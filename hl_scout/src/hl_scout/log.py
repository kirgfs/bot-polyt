"""Structured JSON logging with secret masking."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

import structlog

# Telegram bot tokens and 32-byte hex secrets (private keys). Tx hashes share the 64-hex shape and get
# masked too — acceptable, the scout never needs them in logs. 20-byte addresses are public and kept.
_SECRET_PATTERNS = [
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\b0x[0-9a-fA-F]{64}\b"),
]


def mask_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("***", text)
    return text


def _mask_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            event_dict[key] = mask_secrets(value)
    return event_dict


def setup_logging(level: str = "INFO", file: str | None = None) -> None:
    """Console (human-readable) + optional JSON file. Safe to call more than once."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if file:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(file, encoding="utf-8"))
    logging.basicConfig(level=level.upper(), handlers=handlers, format="%(message)s", force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _mask_processor,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
