"""Telegram commands: parsing, who gets an answer, offsets, and the /action and /report texts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from polybot.core.timeutil import NS_PER_S
from polybot.minibot.commands import COMMANDS, CommandLoop, parse_command
from polybot.minibot.model import (
    ClosedDay,
    DayStats,
    MarketStatus,
    RecentFill,
    WatchedMatch,
    WatchView,
)
from polybot.minibot.report import Reporter
from polybot.ops.telegram import TelegramNotifier
from tests.minibot_helpers import MIN, T0, Collect, H, mini_config

TOKEN = "123456789:AAH-commands-token-value_for_tests"
NOW_S = T0 // NS_PER_S


class FakeBotApi:
    """getUpdates batches, sendMessage capture, an optional error status for polls."""

    def __init__(self, batches: list[list[dict[str, Any]]] | None = None, status: int = 200):
        self.batches = list(batches or [])
        self.status = status
        self.polls: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content)
        if method == "getUpdates":
            self.polls.append(body)
            if self.status != 200:
                description = "Conflict: terminated by other getUpdates request"
                return httpx.Response(self.status, json={"ok": False, "description": description})
            batch = self.batches.pop(0) if self.batches else []
            return httpx.Response(200, json={"ok": True, "result": batch})
        if method == "sendMessage":
            self.sent.append(body)
        return httpx.Response(200, json={"ok": True, "result": True})


def update(update_id: int, text: str, *, chat: int = 42, date: int = NOW_S) -> dict[str, Any]:
    message = {"message_id": update_id, "date": date, "chat": {"id": chat}, "text": text}
    return {"update_id": update_id, "message": message}


def boom() -> str:
    raise RuntimeError("report failed")


def command_loop(api: FakeBotApi, client: httpx.AsyncClient) -> CommandLoop:
    notifier = TelegramNotifier(client, SecretStr(TOKEN), [42])
    handlers = {"status": lambda: "STATUS", "help": lambda: "HELP", "report": boom}
    return CommandLoop(notifier, handlers, allowed_chats=[42], clock=lambda: T0)


def test_parse_command() -> None:
    assert parse_command("/status") == "status"
    assert parse_command("/Report@polybot_bot today") == "report"
    assert parse_command("/") is None
    assert parse_command("status") is None


def test_menu_follows_telegram_limits() -> None:
    for command, description in COMMANDS:  # docs/api_notes.md §16a
        assert re.fullmatch(r"[a-z0-9_]{1,32}", command)
        assert 1 <= len(description) <= 256


async def test_only_the_operator_gets_answers(capsys: pytest.CaptureFixture[str]) -> None:
    api = FakeBotApi()
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handler)) as client:
        loop = command_loop(api, client)
        await loop.handle(update(1, "/status"))
        await loop.handle(update(2, "/status", chat=777))  # a stranger
        await loop.handle(update(3, "/status", date=NOW_S - 600))  # sent while the bot was down
        await loop.handle(update(4, "hello"))
        await loop.handle(update(5, "/nonsense"))
        await loop.handle(update(6, "/report"))
    assert [(m["chat_id"], m["text"]) for m in api.sent] == [
        (42, "STATUS"),
        (42, "HELP"),
        (42, "HELP"),
        (42, "⚠️ Не получилось собрать ответ, подробности — в логе бота."),
    ]
    out = capsys.readouterr().out
    assert "telegram_command_ignored" in out and "777" in out and "report failed" in out


async def test_offsets_confirm_what_was_handled() -> None:
    api = FakeBotApi([[update(5, "/status"), update(6, "/help")], []])
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handler)) as client:
        loop = command_loop(api, client)
        assert await loop.poll_once() == 2
        assert await loop.poll_once() == 0
    assert "offset" not in api.polls[0] and api.polls[1]["offset"] == 7
    assert api.polls[0]["timeout"] == 30 and api.polls[0]["allowed_updates"] == ["message"]
    assert [m["text"] for m in api.sent] == ["STATUS", "HELP"]


async def test_conflict_is_reported_not_fatal(capsys: pytest.CaptureFixture[str]) -> None:
    api = FakeBotApi(status=409)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handler)) as client:
        assert await command_loop(api, client).poll_once() is None
    out = capsys.readouterr().out
    assert "telegram_poll_rejected" in out and "another process polls this bot" in out
    assert TOKEN not in out


# ------------------------------------------------------------------ texts


def market(event_id: str, **overrides: Any) -> MarketStatus:
    data: dict[str, Any] = {
        "token": "100001",
        "title": "Inter vs. Milan",
        "label": "Will Inter win?",
        "league": "serie-a",
        "start_ns": T0 + 5 * H,
        "phase": "quoting",
        "reason": "",
        "fair": 0.42,
        "bid": "0.39",
        "ask": "0.45",
        "position": "0",
        "rewards_daily": 10.0,
        "reviewed": True,
        "event_id": event_id,
    }
    data.update(overrides)
    return MarketStatus(**data)


def test_action_shows_matches_quotes_reasons_and_fills(tmp_path: Path) -> None:
    reporter = Reporter(Collect(), mini_config(leagues=["serie-a", "ligue-1"]), tmp_path)
    view = WatchView(
        ts_ns=T0,
        polled_ns=T0 - MIN,
        matches=[
            WatchedMatch("e2", "Lyon vs. Nice", "ligue-1", T0 + 30 * H, 3),
            WatchedMatch("e1", "Inter vs. Milan", "serie-a", T0 + 5 * H, 3),
        ],
        markets=[
            market("e1"),
            market("old", title="Ajax vs. PSV", label="Draw?", phase="pulled", position="5"),
        ],
        eligible=4,
        skipped={"too_far": 3, "over_max_markets": 0},
        recent=[
            RecentFill(T0 - 5 * MIN, "Inter vs. Milan", "Will Inter win?", "buy", "0.39", "20")
        ],
    )
    text = reporter.watch_text(view)
    assert "За чем следит бот</b> · 16:00 Ереван" in text
    assert "Матчей: <b>2</b> · подходящих рынков: 4 · котирую: 1" in text
    lines = text.splitlines()
    first = lines.index("21:00 · Серия А · Inter vs. Milan")  # sorted by kickoff
    assert "<code>0.39 × 0.45</code>" in lines[first + 1]
    assert "27.09 22:00 · Лига 1 · Lyon vs. Nice · не котирую" in lines
    assert "<b>Ещё рынки</b>" in text and "поз. +5" in text
    assert "Не котирую рынки: дальше горизонта — 3" in text and "сверх лимита" not in text
    assert "15:55 🟢 купил 20 × 0.39 · Inter vs. Milan" in text


def test_action_explains_an_empty_view(tmp_path: Path) -> None:
    reporter = Reporter(Collect(), mini_config(), tmp_path)
    empty = WatchView(T0, 0, [], [], 0, {}, [])
    assert "Gamma ещё не опрошена" in reporter.watch_text(empty)
    empty.polled_ns = T0
    assert "Gamma не нашла матчей этих лиг" in reporter.watch_text(empty)


def test_report_today_and_help(tmp_path: Path) -> None:
    reporter = Reporter(Collect(), mini_config(), tmp_path)
    today = ClosedDay(DayStats(day="2026-09-26", start_value=200.0), 201.0, 0.0, 200.0, 0)
    text = reporter.daily_text(today, as_of_ns=T0)
    assert text.startswith("📊 <b>Сегодня · 26.09</b> · на 16:00 (Ереван)")
    assert "За день: <b>+$1.00</b>" in text and "Итоги дня придут после 00:00 UTC" in text
    assert "Итоги дня · 26.09" in reporter.daily_text(today)
    help_text = reporter.help_text()
    assert all(f"/{command} — " in help_text for command, _ in COMMANDS)
