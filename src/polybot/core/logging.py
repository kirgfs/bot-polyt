"""structlog setup: JSON lines, UTC timestamps and secret masking (CLAUDE.md, rule 2).

Secrets are held as SecretStr and never passed to the logger on purpose; the masking
processor is a safety net. It masks anything shaped like a key, which includes
condition ids and tx hashes (0x + 64 hex). Log those through `short_id()`.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

_MASK = "***"
_SECRET_KEY = re.compile(
    r"(api_?key|apikey|secret|passphrase|password|token|signature|private|auth|cookie)",
    re.IGNORECASE,
)
# Keys whose names contain "token" but carry public identifiers.
_PUBLIC_KEYS = frozenset({"token_id", "token_ids", "tokens", "n_tokens", "asset_id"})
_HEX_KEY = re.compile(r"0x[0-9a-fA-F]{64,}")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_QUERY_SECRET = re.compile(
    r"((?:api[_-]?key|apikey|token|secret|passphrase|signature)=)[^&\s\"']+", re.IGNORECASE
)
_TOKEN_RUN = re.compile(r"[A-Za-z0-9+/_\-]{32,}={0,2}")
# Shaped like a Telegram bot token as BotFather issues it (digits, colon, long secret), e.g.
# inside a request URL. A safety net only: over-masking a look-alike costs nothing.
_TELEGRAM_TOKEN = re.compile(r"\b\d{5,}:[A-Za-z0-9_\-]{20,}")


def _looks_like_secret(run: str) -> bool:
    # Random base64/hex credentials mix cases and digits; slugs have many hyphens,
    # CLOB token ids are pure digits. Both must stay readable.
    return (
        run.count("-") <= 2
        and any(c.isupper() for c in run)
        and any(c.islower() for c in run)
        and any(c.isdigit() for c in run)
    )


def mask_text(text: str) -> str:
    text = _TELEGRAM_TOKEN.sub(_MASK, text)
    text = _QUERY_SECRET.sub(lambda m: m.group(1) + _MASK, text)
    text = _HEX_KEY.sub(_MASK, text)
    text = _UUID.sub(_MASK, text)
    return _TOKEN_RUN.sub(lambda m: _MASK if _looks_like_secret(m.group(0)) else m.group(0), text)


def short_id(value: str, keep: int = 10) -> str:
    """Loggable prefix of a long public id (condition id, tx hash, token id)."""
    return value if len(value) <= keep else value[:keep] + "…"


def _mask_value(key: str | None, value: Any) -> Any:
    if (
        key is not None
        and key not in _PUBLIC_KEYS
        and _SECRET_KEY.search(key)
        and value not in (None, "", False)
    ):
        return _MASK
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, Mapping):
        return {k: _mask_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_mask_value(None, v) for v in value]
    return value


def mask_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        event_dict[key] = _mask_value(None if key == "event" else key, event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    logging.basicConfig(stream=sys.stdout, level=level.upper(), format="%(message)s")
    # Library chatter (websockets frames, httpx requests) is not useful at INFO,
    # and httpx request lines could carry query-string credentials.
    for noisy in ("websockets", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            mask_secrets,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
