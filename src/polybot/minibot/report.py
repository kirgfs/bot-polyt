"""Mini-bot reports: Telegram messages (HTML, docs/api_notes.md §16a) and markdown files.

Telegram: start and stop, a summary after each UTC day, a short status every few hours,
alerts (daily loss limit, geoblock, a market settled). Files under
`data/reports/minibot/`: `paper_<day>.md` per day and `rules_templates.md` (rule 4).
Times in messages are shown in the operator's zone (Yerevan by default); days are UTC.

A report must never stop the bot: every failure here is logged and swallowed.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from polybot.core.config import MiniBotConfig
from polybot.core.logging import get_logger
from polybot.core.timeutil import NS_PER_S
from polybot.minibot.model import ClosedDay, MarketStatus, Phase, Settlement, Status
from polybot.minibot.selection import Template
from polybot.ops.telegram import Notifier, esc

log = get_logger(__name__)

LEAGUE_NAMES = {
    "serie-a": "Серия А",
    "la-liga": "Ла Лига",
    "ligue-1": "Лига 1",
    "eredivisie": "Эредивизи",
}
PHASE_ICONS = {
    Phase.QUOTING: "🟢",
    Phase.PAUSED: "⏸",
    Phase.WAITING: "⏳",
    Phase.PULLED: "🔚",
    Phase.HALTED: "⛔",
}
REASONS = {
    "no_book": "нет книги",
    "no_quotes": "книга широкая или односторонняя",
    "jump": "скачок цены, пауза",
    "before_kickoff": "снято перед матчем",
    "daily_loss_limit": "дневной лимит убытка",
    "stale_metadata": "нет свежих данных Gamma",
    "deselected": "выбыл из отбора",
    "tick_change": "смена тика",
}
TEMPLATES_FILE = "rules_templates.md"


def league_name(slug: str) -> str:
    return LEAGUE_NAMES.get(slug, slug)


def money(value: float) -> str:
    sign = "−" if value < -0.005 else ""
    return f"{sign}${abs(value):,.2f}"


def signed(value: float) -> str:
    if abs(value) < 0.005:
        return "$0.00"
    return f"{'+' if value > 0 else '−'}${abs(value):,.2f}"


def percent(value: float, base: float) -> str:
    if base <= 0:
        return ""
    share = 100 * value / base
    return f"{'+' if share >= 0 else '−'}{abs(share):.1f}%"


def minutes(total: int) -> str:
    hours, mins = divmod(total, 60)
    return f"{hours} ч {mins:02d} мин" if hours else f"{mins} мин"


class Reporter:
    def __init__(self, notifier: Notifier, cfg: MiniBotConfig, reports_dir: Path) -> None:
        self.notifier = notifier
        self.cfg = cfg
        self.dir = reports_dir
        tg = cfg.telegram
        self.tz = timezone(timedelta(hours=tg.display_utc_offset_h))
        self.tz_label = tg.display_tz_label
        self._templates_written: frozenset[str] = frozenset()

    # ------------------------------------------------------------------ formatting

    def local(self, ts_ns: int) -> datetime:
        return datetime.fromtimestamp(ts_ns / NS_PER_S, tz=self.tz)

    def hhmm(self, ts_ns: int) -> str:
        return self.local(ts_ns).strftime("%H:%M")

    def when(self, ts_ns: int, now: int) -> str:
        """Kickoff time: "21:45" today (local), "27.09 21:45" otherwise."""
        moment, today = self.local(ts_ns), self.local(now)
        return moment.strftime("%H:%M" if moment.date() == today.date() else "%d.%m %H:%M")

    def day_bounds(self, day: str) -> str:
        """The UTC day in local time, e.g. "26.09 04:00 – 27.09 04:00 (Ереван)"."""
        start = datetime.fromisoformat(day).replace(tzinfo=UTC).astimezone(self.tz)
        end = start + timedelta(days=1)
        return f"{start:%d.%m %H:%M} – {end:%d.%m %H:%M} ({self.tz_label})"

    def leagues(self) -> str:
        return ", ".join(league_name(s) for s in self.cfg.leagues)

    def start_text(self, status: Status, restored: bool) -> str:
        tg = self.cfg.telegram
        lines = [
            "🚀 <b>Мини-бот запущен</b> · бумага",
            f"⚽ {esc(self.leagues())}",
            f"💼 Счёт <b>{money(status.value)}</b> · депозит {money(status.deposit)}"
            + (f" · всего {signed(status.pnl_total)}" if restored else ""),
        ]
        plan = []
        if tg.daily_report:
            midnight = self.local(0).strftime("%H:%M")  # 00:00 UTC in local time
            plan.append(f"итоги дня — в {midnight} ({esc(self.tz_label)}), после 00:00 UTC")
        if tg.status_every_h > 0:
            plan.append(f"статус — каждые {tg.status_every_h:g} ч")
        if plan:
            lines.append("🗓 " + "; ".join(plan) + ".")
        return "\n".join(lines)

    def stop_text(self, status: Status, reason: str) -> str:
        return "\n".join(
            [
                f"🛑 <b>Мини-бот остановлен</b> · {esc(reason)}",
                f"💼 Счёт <b>{money(status.value)}</b> · всего {signed(status.pnl_total)}",
                "Бумажные ордера сняты; позиции сохранены до следующего запуска.",
            ]
        )

    def market_line(self, market: MarketStatus, now: int) -> str:
        phase = Phase(market.phase)
        icon = PHASE_ICONS.get(phase, "•")
        head = f"{icon} {esc(market.title)} — <i>{esc(market.label)}</i>"
        details: list[str] = []
        if phase is Phase.QUOTING and (market.bid or market.ask):
            details.append(f"<code>{market.bid or '—'} × {market.ask or '—'}</code>")
        elif market.reason:
            details.append(esc(REASONS.get(market.reason, market.reason)))
        position = float(market.position)
        if position:
            details.append(f"поз. {position:+g}".replace("-", "−"))
        details.append(f"старт {self.when(market.start_ns, now)}")
        if not market.reviewed:
            details.append("правила ⚠️")
        return head + "\n    " + " · ".join(details)

    def status_text(self, status: Status) -> str:
        quoting = sum(1 for m in status.markets if m.phase == Phase.QUOTING)
        icon = "⛔" if status.halted else "🟢"
        lines = [
            f"{icon} <b>Мини-бот</b> · {self.hhmm(status.ts_ns)} {esc(self.tz_label)} · бумага",
            f"💼 <b>{money(status.value)}</b> · день {signed(status.pnl_day)}"
            f" · всего {signed(status.pnl_total)}",
            f"🔒 В ордерах {money(status.locked)} · свободно {money(status.cash - status.locked)}",
            f"🔁 За день: {status.fills_day} сд., оборот {money(status.volume_day)}"
            f" · награды ≈ {money(status.rewards_day)}",
        ]
        if status.halted:
            lines.append("⛔ Дневной лимит убытка: котировки сняты до конца суток UTC.")
        if status.markets:
            lines += ["", f"<b>Рынки</b> · котирую {quoting} из {len(status.markets)}"]
            lines += [self.market_line(m, status.ts_ns) for m in status.markets]
        else:
            lines += ["", "Подходящих рынков сейчас нет."]
        if status.unsettled:
            lines.append(f"🏁 Ждут расчёта: {status.unsettled}")
        return "\n".join(lines)

    def daily_text(self, closed: ClosedDay) -> str:
        day, end = closed.stats, closed.end_value
        pnl_day, pnl_total = end - day.start_value, end - closed.deposit
        rebates = closed.rebates_total - day.rebates_at_start
        date_label = datetime.fromisoformat(day.day).strftime("%d.%m")
        lines = [
            f"📊 <b>Итоги дня · {date_label}</b> · бумага",
            f"<i>{esc(self.day_bounds(day.day))}</i>",
            "",
            f"💼 Счёт: <b>{money(end)}</b>",
            f"📈 За день: <b>{signed(pnl_day)}</b> ({percent(pnl_day, day.start_value)})",
            f"🏦 С начала: <b>{signed(pnl_total)}</b> ({percent(pnl_total, closed.deposit)})"
            f" · депозит {money(closed.deposit)}",
            "",
            f"🔁 Сделок: <b>{day.fills}</b> · оборот {money(day.volume_usd)}",
            f"💸 Ребейты ≈ {money(rebates)} · 🎁 награды ≈ {money(day.rewards_usd)}",
        ]
        if day.settlements:
            lines.append(
                f"🏁 Рассчитано рынков: {len(day.settlements)} · P&amp;L {signed(day.settled_pnl)}"
            )
        active = sorted(day.markets.values(), key=lambda m: (-m.volume_usd, -m.quoted_min))
        active = [m for m in active if m.fills or m.quoted_min]
        if active:
            lines += ["", "<b>По рынкам</b>"]
            for m in active[:12]:
                league = f"{esc(league_name(m.league))} · " if m.league else ""
                lines.append(
                    f"⚽ {league}{esc(m.title)} — <i>{esc(m.label)}</i>\n"
                    f"    {m.fills} сд., {money(m.volume_usd)}"
                    f" · награды ≈ {money(m.rewards_usd)} · в книге {minutes(m.quoted_min)}"
                )
            if len(active) > 12:
                lines.append(f"… и ещё {len(active) - 12}")
        if closed.unreviewed:
            lines += [
                "",
                f"⚠️ Правила {closed.unreviewed} рынк. не одобрены — допустимо только на бумаге.",
            ]
        if day.halted:
            lines.append("⛔ Срабатывал дневной лимит убытка.")
        lines += ["", "<i>Награды и ребейты — оценка по книге, не выплата.</i>"]
        return "\n".join(lines)

    def daily_markdown(self, closed: ClosedDay) -> str:
        day, end = closed.stats, closed.end_value
        pnl_day, pnl_total = end - day.start_value, end - closed.deposit
        rebates = closed.rebates_total - day.rebates_at_start
        out = [
            f"# Мини-бот (бумага): итоги {day.day} UTC",
            "",
            f"Сутки: {self.day_bounds(day.day)}. Лиги: {self.leagues()}.",
            "",
            "| Показатель | Значение |",
            "|---|---|",
            f"| Счёт на конец дня | {money(end)} |",
            f"| Счёт на начало дня | {money(day.start_value)} |",
            f"| P&L за день | {signed(pnl_day)} ({percent(pnl_day, day.start_value)}) |",
            f"| P&L с начала (депозит {money(closed.deposit)}) | {signed(pnl_total)} |",
            f"| Сделок | {day.fills} |",
            f"| Оборот | {money(day.volume_usd)} |",
            f"| Ребейты (оценка) | {money(rebates)} |",
            f"| Награды за ликвидность (оценка) | {money(day.rewards_usd)} |",
            f"| Рассчитано рынков | {len(day.settlements)}, P&L {signed(day.settled_pnl)} |",
            f"| Дневной лимит убытка | {'сработал' if day.halted else 'нет'} |",
            f"| Рынков с неодобренными правилами | {closed.unreviewed} |",
            "",
        ]
        if day.markets:
            out += [
                "## По рынкам",
                "",
                "| Лига | Матч | Рынок | Сделок | Оборот | Награды | В книге |",
                "|---|---|---|---|---|---|---|",
            ]
            for m in sorted(day.markets.values(), key=lambda m: -m.volume_usd):
                out.append(
                    f"| {league_name(m.league)} | {m.title} | {m.label} | {m.fills} |"
                    f" {money(m.volume_usd)} | {money(m.rewards_usd)} | {minutes(m.quoted_min)} |"
                )
            out.append("")
        if day.settlements:
            out += ["## Расчёты", "", "| Матч | Рынок | Итог | P&L |", "|---|---|---|---|"]
            for s in day.settlements:
                out.append(
                    f"| {s.title} | {s.label} | {outcome_word(s.outcome)} | {signed(s.pnl)} |"
                )
            out.append("")
        out += [
            "Награды — оценка по доле в видимой книге (нижняя граница, docs/api_notes.md §9);",
            "ребейты — по параметрам feeSchedule рынка (§8). Это не выплаты биржи.",
        ]
        return "\n".join(out) + "\n"

    def settled_text(self, s: Settlement) -> str:
        return (
            f"🏁 <b>Рассчитан рынок</b> · {esc(s.title)}\n"
            f"<i>{esc(s.label)}</i> → <b>{outcome_word(s.outcome)}</b>\n"
            f"P&amp;L по рынку за всё время: <b>{signed(s.pnl)}</b>"
        )

    def loss_limit_text(self, value: float, pnl_day: float) -> str:
        return (
            "⛔ <b>Дневной лимит убытка</b>\n"
            f"Счёт {money(value)} · за день {signed(pnl_day)}"
            f" (лимит {money(self.cfg.risk.daily_loss_limit_usd)})\n"
            "Котировки сняты до конца суток UTC."
        )

    def geoblock_text(self, verdict: str) -> str:
        return (
            f"🌍 <b>Geoblock</b>: {esc(verdict)}\n"
            "Бот остановлен, бумажные ордера сняты (правило 7)."
        )

    def startup_failed_text(self, error: str) -> str:
        return (
            "⚠️ <b>Мини-бот не стартует</b>\n"
            f"<code>{esc(error[:500])}</code>\n"
            "Логи: <code>docker compose logs --tail 50 minibot</code>"
        )

    # ------------------------------------------------------------------ delivery

    async def send(self, text: str, *, silent: bool = False) -> None:
        if not self.cfg.telegram.enabled:
            return
        try:
            await self.notifier.send(text, silent=silent)
        except Exception as exc:  # a report must never stop the bot
            log.warning("minibot_report_send_failed", error=repr(exc))

    async def started(self, status: Status, *, restored: bool) -> None:
        await self.send(self.start_text(status, restored))

    async def stopped(self, status: Status, reason: str) -> None:
        await self.send(self.stop_text(status, reason))

    async def status(self, status: Status) -> None:
        await self.send(self.status_text(status), silent=True)

    async def day_closed(self, closed: ClosedDay) -> None:
        try:
            _write_atomic(self.dir / f"paper_{closed.stats.day}.md", self.daily_markdown(closed))
        except OSError as exc:
            log.warning("minibot_daily_report_write_failed", error=repr(exc))
        if self.cfg.telegram.daily_report:
            await self.send(self.daily_text(closed), silent=True)

    async def settled(self, settlement: Settlement) -> None:
        await self.send(self.settled_text(settlement), silent=True)

    async def loss_limit(self, value: float, pnl_day: float) -> None:
        await self.send(self.loss_limit_text(value, pnl_day))

    async def geoblock(self, verdict: str) -> None:
        await self.send(self.geoblock_text(verdict))

    async def startup_failed(self, error: str) -> None:
        await self.send(self.startup_failed_text(error))

    def write_templates(self, templates: dict[str, Template], approved: Iterable[str]) -> None:
        """Rules templates for review (CLAUDE.md, rule 4).

        Rewritten when the set changes or the file is gone (publish_reports.sh moves report
        files to the reports repository).
        """
        approved_set = frozenset(approved)
        key = frozenset(templates) | frozenset(f"approved:{t}" for t in approved_set)
        if key == self._templates_written and (self.dir / TEMPLATES_FILE).exists():
            return
        out = [
            "# Шаблоны правил резолюции: мини-бот",
            "",
            "Шаблон — текст `description` рынка, в котором названия команд, даты и числа",
            "заменены. Чтобы одобрить шаблон: прочитать пример и добавить id в",
            "`config/minibot.yaml` → `rules.approved_templates` (правило 4).",
            f"Обновлено: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC.",
            "",
        ]
        for template_id, template in sorted(templates.items()):
            state = "одобрен" if template_id in approved_set else "**не одобрен**"
            out += [
                f"## `{template_id}` — {state}",
                "",
                f"Пример: {template.example_title}",
                "",
                *[
                    f"> {line}" if line else ">"
                    for line in template.example_description.splitlines()
                ],
                "",
                "Шаблон:",
                "",
                "```",
                template.text,
                "```",
                "",
            ]
        try:
            _write_atomic(self.dir / TEMPLATES_FILE, "\n".join(out))
        except OSError as exc:
            log.warning("minibot_templates_write_failed", error=repr(exc))
            return
        self._templates_written = key


def outcome_word(outcome: str) -> str:
    return {"yes": "Да", "no": "Нет"}.get(outcome, f"выплата {outcome}")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
