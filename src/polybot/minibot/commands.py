"""Telegram commands of the mini-bot (docs/api_notes.md §16a, docs/architecture.md §13).

`/status`, `/report`, `/action`, `/help`. Long polling, so the server needs no inbound
port. Only chats listed in TELEGRAM_ALLOWED_CHAT_IDS get an answer; other chats are
ignored and logged by id. Every command only reads the bot's state: none can change it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from polybot.core.logging import get_logger
from polybot.core.timeutil import NS_PER_S, mono_ns, now_ns
from polybot.ops.telegram import TelegramNotifier

log = get_logger(__name__)

# (command, menu description): lowercase Latin, digits and "_", 1-32 characters (§16a).
COMMANDS: tuple[tuple[str, str], ...] = (
    ("status", "Счёт, P&L и рынки сейчас"),
    ("report", "Отчёт за сегодня"),
    ("action", "За какими матчами бот следит"),
    ("help", "Список команд"),
)
MAX_AGE_S = 120  # commands sent while the bot was down are not answered after a restart
RETRY_MIN_S = 15.0
RETRY_MAX_S = 300.0
MIN_POLL_S = 1.0  # an empty answer faster than this is not long polling: pause, do not spin

Handler = Callable[[], str]


def parse_command(text: str) -> str | None:
    """Command name of a message: "/Status@my_bot now" → "status"; None for plain text."""
    if not text.startswith("/") or len(text) < 2:
        return None
    word = text[1:].split(maxsplit=1)[0] if text[1:].strip() else ""
    name = word.split("@", 1)[0].lower()
    return name or None


class CommandLoop:
    def __init__(
        self,
        telegram: TelegramNotifier,
        handlers: Mapping[str, Handler],
        *,
        allowed_chats: Sequence[int],
        poll_timeout_s: int = 30,
        clock: Callable[[], int] = now_ns,
    ) -> None:
        self.telegram = telegram
        self.handlers = handlers
        self.allowed = frozenset(allowed_chats)
        self.poll_timeout_s = poll_timeout_s
        self.clock = clock
        self.offset: int | None = None
        self.answered = 0

    async def run(self) -> None:
        await self.telegram.set_commands(COMMANDS)
        failures = 0
        while True:
            started = mono_ns()
            handled = await self.poll_once()
            if handled is None:
                failures += 1
                await asyncio.sleep(min(RETRY_MAX_S, RETRY_MIN_S * 2 ** (failures - 1)))
                continue
            failures = 0
            if not handled and mono_ns() - started < MIN_POLL_S * NS_PER_S:
                await asyncio.sleep(MIN_POLL_S)

    async def poll_once(self) -> int | None:
        """One long poll; the number of updates handled, None when the poll failed."""
        updates = await self.telegram.get_updates(self.offset, self.poll_timeout_s)
        if updates is None:
            return None
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self.offset = max(self.offset or 0, update_id + 1)
            await self.handle(update)
        return len(updates)

    async def handle(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        text = message.get("text")
        if not isinstance(chat_id, int) or not isinstance(text, str):
            return
        if chat_id not in self.allowed:
            log.info("telegram_command_ignored", chat_id=chat_id)
            return
        sent = message.get("date")
        if isinstance(sent, int) and self.clock() / NS_PER_S - sent > MAX_AGE_S:
            return
        command = parse_command(text) or "help"
        handler = self.handlers.get(command) or self.handlers["help"]
        try:
            answer = handler()
        except Exception as exc:  # a broken report must not stop the command loop
            log.warning("telegram_command_failed", command=command, error=repr(exc))
            answer = "⚠️ Не получилось собрать ответ, подробности — в логе бота."
        await self.telegram.reply(chat_id, answer)
        self.answered += 1
        log.info("telegram_command", command=command)
