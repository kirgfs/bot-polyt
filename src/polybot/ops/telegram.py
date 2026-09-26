"""Telegram reports for the mini-bot (docs/api_notes.md §16a): outbound messages only.

The token sits in the request path, so no URL is ever logged: only the response code and
Telegram's `description` (CLAUDE.md, rule 2). A failed message never stops the bot.
"""

from __future__ import annotations

import asyncio
import html
from collections.abc import Sequence
from typing import Protocol

import httpx
from pydantic import SecretStr

from polybot.core.logging import get_logger

log = get_logger(__name__)

API_BASE = "https://api.telegram.org/bot"
MAX_TEXT = 4096  # characters after entity parsing
MAX_RETRY_AFTER_S = 60.0


def esc(text: object) -> str:
    """Escape user-visible text for HTML parse mode."""
    return html.escape(str(text), quote=False)


def split_message(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split on line boundaries; every line keeps its own tags balanced."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = line[:limit]
    if current:
        chunks.append(current)
    return chunks


class Notifier(Protocol):
    async def send(self, text: str, *, silent: bool = False) -> None: ...


class NullNotifier:
    """Used when Telegram is not configured: messages go nowhere."""

    async def send(self, text: str, *, silent: bool = False) -> None:
        return None


class TelegramNotifier:
    def __init__(
        self,
        client: httpx.AsyncClient,
        token: SecretStr,
        chat_ids: Sequence[int],
        *,
        base_url: str = API_BASE,
    ) -> None:
        self._client = client
        secret = token.get_secret_value().strip()
        if not secret or any(c.isspace() or c in "/?#" for c in secret):
            raise ValueError("TELEGRAM_BOT_TOKEN is empty or has characters a URL path cannot hold")
        self._url = f"{base_url}{secret}/sendMessage"
        self._chat_ids = tuple(chat_ids)
        self.sent = 0
        self.failed = 0

    async def send(self, text: str, *, silent: bool = False) -> None:
        for chunk in split_message(text):
            for chat_id in self._chat_ids:
                await self._send_one(chat_id, chunk, silent)

    async def _send_one(self, chat_id: int, text: str, silent: bool) -> None:
        body = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": silent,
            "link_preview_options": {"is_disabled": True},
        }
        for attempt in range(2):
            try:
                response = await self._client.post(self._url, json=body, timeout=15.0)
            except Exception as exc:  # only the type: an error message may quote the URL
                self.failed += 1
                log.warning("telegram_send_failed", error=type(exc).__name__)
                return
            if response.status_code == 200:
                self.sent += 1
                return
            description, retry_after = _error_details(response)
            if response.status_code == 429 and retry_after and attempt == 0:
                await asyncio.sleep(min(retry_after, MAX_RETRY_AFTER_S))
                continue
            self.failed += 1
            log.warning(
                "telegram_send_rejected",
                status=response.status_code,
                description=description,
                hint="403: press Start in the bot chat; 401/404: wrong token"
                if response.status_code in (401, 403, 404)
                else None,
            )
            return


def _error_details(response: httpx.Response) -> tuple[str, float | None]:
    try:
        data = response.json()
    except ValueError:
        return "", None
    if not isinstance(data, dict):
        return "", None
    params = data.get("parameters")
    retry = params.get("retry_after") if isinstance(params, dict) else None
    retry_after = float(retry) if isinstance(retry, int | float) else None
    return str(data.get("description") or ""), retry_after
