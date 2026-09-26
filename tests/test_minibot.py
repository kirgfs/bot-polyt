"""Mini-bot pieces: selection, rules templates, reports, Telegram delivery, retention."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest
from pydantic import SecretStr

from polybot.data.records import Kind, Record, records_to_table
from polybot.minibot.app import prune_raw, status_slot
from polybot.minibot.model import ClosedDay, DayStats, MarketDay, MarketStatus, Settlement, Status
from polybot.minibot.report import Reporter, money, signed
from polybot.minibot.rules import rules_template, team_names
from polybot.minibot.selection import select
from polybot.ops.telegram import MAX_TEXT, TelegramNotifier, split_message
from tests.minibot_helpers import (
    DESCRIPTION,
    MIN,
    T0,
    Collect,
    H,
    mini_config,
    pairs,
    raw_event,
)

# ------------------------------------------------------------------ selection and rules


def test_selection_takes_yes_tokens_by_rewards_then_kickoff() -> None:
    cfg = mini_config(selection={"max_markets": 4})
    result = select(
        pairs(raw_event("1000", T0 + 6 * H), raw_event("2000", T0 + 3 * H, rewards=False)),
        cfg,
        T0,
        paper=True,
    )
    assert [c.token for c in result.chosen] == ["100021", "100011", "100001", "200001"]
    assert result.skipped["over_max_markets"] == 2
    assert all(c.league == "serie-a" and not c.reviewed for c in result.chosen)
    first = result.chosen[0]
    assert first.rewards is not None and first.rewards.max_spread == pytest.approx(0.035)


def test_selection_skips_what_it_cannot_quote() -> None:
    cfg = mini_config()
    too_close = raw_event("1000", T0 + 90 * MIN)  # pull at −75 min leaves < 30 min
    too_far = raw_event("2000", T0 + 60 * H)
    names = raw_event("3000", T0 + 6 * H, outcomes=("Inter", "Milan"))
    no_rules = raw_event("4000", T0 + 6 * H)
    for market in no_rules["markets"]:
        market["description"] = ""
    result = select(pairs(too_close, too_far, names, no_rules), cfg, T0, paper=True)
    assert not result.chosen
    assert result.skipped == {
        "too_close_to_start": 3,
        "too_far": 3,
        "not_yes_no": 3,
        "no_rules_text": 3,
        "over_max_markets": 0,
    }


def test_quoted_market_stays_until_the_pull() -> None:
    cfg = mini_config(selection={"max_markets": 1})
    near = raw_event("1000", T0 + 100 * MIN)  # 25 min of quoting left: too little for a new one
    far = raw_event("2000", T0 + 6 * H)
    fresh = select(pairs(near, far), cfg, T0, paper=True)
    assert [c.token for c in fresh.chosen] == ["200021"]
    kept = select(pairs(near, far), cfg, T0, paper=True, active=frozenset({"100001"}))
    assert [c.token for c in kept.chosen] == ["100001"]


def test_rules_template_ignores_teams_and_dates() -> None:
    one = rules_template(DESCRIPTION.format(side="Inter"), team_names("Inter vs. Milan"))
    two = rules_template(
        DESCRIPTION.format(side="Ajax").replace("September 27", "October 4"),
        team_names("Ajax vs. PSV", "Ajax", "PSV"),
    )
    assert one[0] == two[0]
    assert "<team>" in one[1] and "<month>" in one[1] and "inter" not in one[1]
    draw = rules_template("If the game ends in a draw, this market resolves to Yes.", [])
    assert draw[0] != one[0]


# ------------------------------------------------------------------ reports


def status(**overrides: object) -> Status:
    market = MarketStatus(
        token="100001",
        title="Inter & Co <b> vs. Milan",
        label="Will Inter win?",
        league="serie-a",
        start_ns=T0 + 5 * H,
        phase="quoting",
        reason="",
        fair=0.42,
        bid="0.39",
        ask="0.45",
        position="20",
        rewards_daily=10.0,
        reviewed=False,
    )
    data: dict[str, object] = {
        "ts_ns": T0,
        "day": "2026-09-26",
        "deposit": 200.0,
        "value": 203.456,
        "day_start_value": 201.0,
        "cash": 180.0,
        "locked": 12.5,
        "rebates_total": 0.12,
        "rewards_day": 0.5,
        "fills_day": 3,
        "volume_day": 25.0,
        "halted": False,
        "unsettled": 1,
        "markets": [market],
    }
    data.update(overrides)
    return Status(**data)  # type: ignore[arg-type]


def test_money_formatting() -> None:
    assert money(1234.5) == "$1,234.50" and money(-3.456) == "−$3.46"
    assert signed(2) == "+$2.00" and signed(-0.5) == "−$0.50" and signed(0.001) == "$0.00"


def test_status_message_is_escaped_html_in_yerevan_time(tmp_path: Path) -> None:
    reporter = Reporter(Collect(), mini_config(), tmp_path)
    text = reporter.status_text(status())
    assert "16:00 Ереван" in text  # 12:00 UTC
    assert "Inter &amp; Co &lt;b&gt; vs. Milan" in text and "<b> vs." not in text
    assert "<code>0.39 × 0.45</code>" in text and "поз. +20" in text
    assert "старт 21:00" in text and "правила ⚠️" in text
    assert "день +$2.46" in text and "всего +$3.46" in text
    assert "Ждут расчёта: 1" in text and len(text) < MAX_TEXT


def test_daily_report_text_and_markdown(tmp_path: Path) -> None:
    stats = DayStats(
        day="2026-09-26",
        start_value=200.0,
        fills=4,
        volume_usd=31.2,
        rewards_usd=0.8,
        settled_pnl=1.5,
        settlements=[Settlement("Inter vs. Milan", "Will Inter win?", "yes", 1.5)],
        markets={
            "100001": MarketDay("Inter vs. Milan", "Will Inter win?", "serie-a", 4, 31.2, 0.8, 95)
        },
    )
    closed = ClosedDay(stats, end_value=203.0, rebates_total=0.3, deposit=200.0, unreviewed=2)
    reporter = Reporter(Collect(), mini_config(), tmp_path)
    text = reporter.daily_text(closed)
    assert "Итоги дня · 26.09" in text and "26.09 04:00 – 27.09 04:00 (Ереван)" in text
    assert "За день: <b>+$3.00</b> (+1.5%)" in text
    assert "Серия А · Inter vs. Milan" in text and "в книге 1 ч 35 мин" in text
    assert "Правила 2 рынк. не одобрены" in text and "P&amp;L +$1.50" in text
    markdown = reporter.daily_markdown(closed)
    assert (
        "| Сделок | 4 |" in markdown
        and "| Inter vs. Milan | Will Inter win? | Да | +$1.50 |" in markdown
    )


async def test_reporter_writes_files_and_never_raises(tmp_path: Path) -> None:
    class Broken:
        async def send(self, text: str, *, silent: bool = False) -> None:
            raise RuntimeError("network down")

    reporter = Reporter(Broken(), mini_config(), tmp_path / "reports")
    stats = DayStats(day="2026-09-26", start_value=200.0)
    await reporter.day_closed(ClosedDay(stats, 200.0, 0.0, 200.0, 0))
    assert (tmp_path / "reports" / "paper_2026-09-26.md").exists()


def test_status_periods_start_at_fixed_local_hours() -> None:
    cfg = mini_config()  # every 6 h, UTC+4: periods start at 02:00, 08:00, 14:00, 20:00 UTC
    before, after = T0 + 1 * H + 59 * MIN, T0 + 2 * H + 1 * MIN  # 13:59 and 14:01 UTC
    assert status_slot(before, cfg) + 1 == status_slot(after, cfg)
    assert status_slot(T0, cfg) == status_slot(before, cfg)


# ------------------------------------------------------------------ Telegram


def test_split_message_keeps_lines_whole() -> None:
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = split_message(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks) and "\n".join(chunks) == text


async def test_telegram_sends_html_and_retries_once_on_429(
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "123456789:AAH-secret-token-value_for_tests-xyz"
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/bot{token}/sendMessage"
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0.01}})
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = TelegramNotifier(client, SecretStr(token), [42])
        await notifier.send("<b>ok</b>", silent=True)
    assert notifier.sent == 1 and notifier.failed == 0 and len(calls) == 2
    assert calls[1] == {
        "chat_id": 42,
        "text": "<b>ok</b>",
        "parse_mode": "HTML",
        "disable_notification": True,
        "link_preview_options": {"is_disabled": True},
    }


async def test_telegram_errors_never_log_the_token(capsys: pytest.CaptureFixture[str]) -> None:
    token = "123456789:AAH-secret-token-value_for_tests-xyz"

    def handler(request: httpx.Request) -> httpx.Response:
        if b"boom" in request.content:
            raise httpx.ConnectError(f"cannot reach {request.url}")
        return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = TelegramNotifier(client, SecretStr(token), [42])
        await notifier.send("hello")
        await notifier.send("boom")
    assert notifier.failed == 2
    out = capsys.readouterr().out
    assert "telegram_send_rejected" in out and "press Start" in out
    assert "secret-token" not in out and "123456789" not in out
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        TelegramNotifier(client, SecretStr("12345:abc def"), [42])


# ------------------------------------------------------------------ retention


def test_prune_keeps_paper_records_and_recent_days(tmp_path: Path) -> None:
    # Dates far in the past: compaction refuses the real current day.
    for day in ("2020-01-06", "2020-01-08", "2020-01-09", "2020-01-10"):
        for source in ("clob_market_ws", "paper"):
            directory = tmp_path / f"date={day}" / f"source={source}"
            directory.mkdir(parents=True)
            for part in ("part-100000-a-000000000001", "part-100500-a-000000000002"):
                row = Record(ts_recv_ns=T0, source=source, kind=Kind.CONTROL, payload="{}")
                pq.write_table(records_to_table([row]), directory / f"{part}.parquet")
    deleted = prune_raw(tmp_path, 2, today=date(2020, 1, 10))
    assert deleted == ["2020-01-06"]
    assert not (tmp_path / "date=2020-01-06" / "source=clob_market_ws").exists()
    assert (tmp_path / "date=2020-01-06" / "source=paper").exists()
    assert (tmp_path / "date=2020-01-08" / "source=clob_market_ws").exists()
    compacted = list((tmp_path / "date=2020-01-09" / "source=paper").glob("compacted-*.parquet"))
    assert len(compacted) == 1  # finished days are merged into hourly files
    assert len(list((tmp_path / "date=2020-01-10" / "source=paper").iterdir())) == 2  # "today"
