"""Mini-bot orchestration (decision 9, docs/architecture.md §13). Paper only.

Geoblock gate, then: Gamma discovery of the configured leagues → market selection → market
WS books of the chosen Yes tokens → stage-0 quotes executed by the paper venue → status
file, state file, Telegram reports. `--dry-run` checks the configuration against Gamma
without quoting (league slugs, market types, outcomes, the current selection).

Exit codes as the recorder: 0 stopped, 1 a component died, 2 geoblock, 3 configuration
or startup failure.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from polybot.analytics.stats import md_table
from polybot.core.config import BaseConfig, MiniBotConfig, Settings
from polybot.core.http import make_client
from polybot.core.logging import get_logger
from polybot.core.memory import process_memory
from polybot.core.timeutil import NS_PER_S, now_ns, ns_to_date
from polybot.data.records import Kind, Record, RecordWriter, Source
from polybot.data.sink import ParquetSink, compact_day
from polybot.execution.paper import PaperVenue
from polybot.minibot.engine import Engine
from polybot.minibot.model import ClosedDay, Status
from polybot.minibot.report import Reporter, league_name
from polybot.minibot.selection import SelectionResult, select
from polybot.ops.daily import delete_day, raw_days
from polybot.ops.telegram import Notifier, NullNotifier, TelegramNotifier
from polybot.recorder.app import (
    EXIT_CRASH,
    EXIT_GEOBLOCK,
    EXIT_OK,
    EXIT_STARTUP,
    install_signal_handlers,
    supervise,
)
from polybot.recorder.discovery import Discovery
from polybot.venues.polymarket.clob_ws import MarketPool
from polybot.venues.polymarket.gamma import GammaClient, GammaError
from polybot.venues.polymarket.geoblock import GeoGuard, GeoStatus, check_geoblock
from polybot.venues.polymarket.markets import (
    ParseIssues,
    PmEvent,
    parse_event,
    recordable_markets,
)
from polybot.venues.polymarket.orderbook import BookTracker

log = get_logger(__name__)

RAW_DIR = Path("minibot") / "raw"
REPORTS_DIR = Path("reports") / "minibot"
KEEP_SOURCES = (Source.PAPER.value,)
SELECTION_ALERT_AFTER = 5
STATUS_LOG_EVERY = 20  # status intervals between log lines and Parquet status rows
HOUSEKEEPING_S = 3600.0
ALERTS_FILE = "minibot_alerts.json"
LIFECYCLE_ALERT_EVERY_S = 600.0  # start and crash messages: at most one per 10 min
REFUSAL_ALERT_EVERY_S = 6 * 3600.0  # geoblock or configuration refusals: every 6 h

CoroutineFactory = Callable[[], Coroutine[Any, Any, Any]]


class AlertThrottle:
    """Lifecycle alerts across restarts: a crash or refusal loop must not flood Telegram."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def allow(self, key: str, every_s: float) -> bool:
        try:
            sent = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            sent = {}
        if not isinstance(sent, dict):
            sent = {}
        now = now_ns()
        last = sent.get(key)
        if isinstance(last, int) and now - last < every_s * NS_PER_S:
            return False
        sent[key] = now
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(sent), encoding="utf-8")
        except OSError as exc:
            log.warning("minibot_alert_state_write_failed", error=repr(exc))
        return True


def make_notifier(settings: Settings, cfg: MiniBotConfig, http: httpx.AsyncClient) -> Notifier:
    token, chats = settings.telegram_bot_token, settings.telegram_chats
    if not cfg.telegram.enabled:
        return NullNotifier()
    if token is None or not chats:
        log.warning(
            "telegram_not_configured",
            hint="set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS in .env",
        )
        return NullNotifier()
    try:
        return TelegramNotifier(http, token, chats)
    except ValueError as exc:
        log.warning("telegram_not_configured", error=str(exc))
        return NullNotifier()


def prune_raw(root: Path, keep_days: int, *, today: date | None = None) -> list[str]:
    """Compact finished days, delete those older than `keep_days` (paper records stay)."""
    today = today or ns_to_date(now_ns())
    deleted = []
    for day in raw_days(root):
        if day >= today:
            continue
        compact_day(root, day.isoformat())
        if day < today - timedelta(days=keep_days) and delete_day(
            root / f"date={day.isoformat()}", KEEP_SOURCES
        ):
            deleted.append(day.isoformat())
    return deleted


async def housekeeping_loop(root: Path, keep_days: int) -> None:
    while True:
        try:
            deleted = await asyncio.to_thread(prune_raw, root, keep_days)
        except OSError as exc:
            log.warning("minibot_housekeeping_failed", error=repr(exc))
        else:
            if deleted:
                log.info("minibot_raw_deleted", days=deleted)
        await asyncio.sleep(HOUSEKEEPING_S)


async def selection_loop(
    discovery: Discovery,
    engine: Engine,
    pool: MarketPool,
    reporter: Reporter,
    cfg: MiniBotConfig,
) -> None:
    failures = 0
    while True:
        try:
            result = await discovery.poll()
        except (GammaError, httpx.HTTPError, json.JSONDecodeError) as exc:
            failures += 1
            report = log.critical if failures >= SELECTION_ALERT_AFTER else log.warning
            report("minibot_selection_failed", error=repr(exc), consecutive=failures)
        else:
            failures = 0
            selection = select(
                result.selected, cfg, now_ns(), paper=True, active=engine.active_tokens()
            )
            tokens = await engine.apply_selection(selection.chosen)
            await pool.set_assets(tokens)
            reporter.write_templates(selection.templates, cfg.rules.approved_templates)
            log.info(
                "minibot_selection",
                chosen=len(selection.chosen),
                eligible=selection.eligible,
                skipped=dict(selection.skipped),
                subscribed=len(tokens),
            )
        await asyncio.sleep(cfg.selection.refresh_s)


def status_slot(ts_ns: int, cfg: MiniBotConfig) -> int:
    """Index of the Telegram status period: periods start at fixed local times (00, 06…)."""
    tg = cfg.telegram
    local_h = ts_ns / (3600 * NS_PER_S) + tg.display_utc_offset_h
    return int(local_h // tg.status_every_h) if tg.status_every_h > 0 else 0


async def status_loop(
    engine: Engine,
    reporter: Reporter,
    extra: Callable[[], dict[str, Any]],
    sink: RecordWriter,
    cfg: MiniBotConfig,
) -> None:
    last_slot = status_slot(now_ns(), cfg)
    tick = 0
    while True:
        await asyncio.sleep(cfg.status_interval_s)
        status = engine.write_status(extra())
        engine.save_state()
        tick += 1
        if tick % STATUS_LOG_EVERY == 0:
            log.info(
                "minibot_status",
                value=round(status.value, 2),
                pnl_day=round(status.pnl_day, 2),
                pnl_total=round(status.pnl_total, 2),
                quoting=sum(1 for m in status.markets if m.phase == "quoting"),
                markets=len(status.markets),
                memory=status.extra.get("memory"),
            )
            snapshot = status.to_dict()
            for key in status.extra:
                snapshot.pop(key, None)
            sink.write(
                Record(
                    ts_recv_ns=status.ts_ns,
                    source=Source.PAPER,
                    kind=Kind.CONTROL,
                    event_type="status",
                    payload=json.dumps(snapshot, default=str, ensure_ascii=False),
                )
            )
        slot = status_slot(status.ts_ns, cfg)
        if cfg.telegram.status_every_h > 0 and slot != last_slot:
            last_slot = slot
            await reporter.status(status)


async def run_minibot(settings: Settings, base: BaseConfig, cfg: MiniBotConfig) -> int:
    if settings.mode == "live" or settings.live_trading:
        # The mini-bot has no path to real orders; refuse rather than let MODE=live suggest
        # that money is at work (CLAUDE.md, rule 1).
        log.critical("minibot_paper_only", mode=settings.mode, live_trading=settings.live_trading)
        return EXIT_STARTUP
    root = settings.data_dir / RAW_DIR
    sink = ParquetSink(
        root,
        flush_interval_s=cfg.sink.flush_interval_s,
        flush_rows=cfg.sink.flush_rows,
        flush_mb=cfg.sink.flush_mb,
        max_buffer_mb=cfg.sink.max_buffer_mb,
        max_buffer_rows=cfg.sink.max_buffer_rows,
        compression_level=cfg.sink.compression_level,
    )
    sink_task = asyncio.create_task(sink.run(), name="sink")
    exit_code = EXIT_OK
    try:
        async with make_client(base.http) as http:
            exit_code = await _run(settings, base, cfg, sink, http)
    finally:
        await sink.close()  # after the flusher's current write, then the rest
        sink_task.cancel()  # the flusher has returned: a no-op safety net
        await asyncio.gather(sink_task, return_exceptions=True)
        log.info("minibot_stopped", exit_code=exit_code, sink=sink.stats.as_dict())
    return exit_code


@dataclass
class Parts:
    discovery: Discovery
    pool: MarketPool
    engine: Engine
    reporter: Reporter
    restored: bool
    late_day: ClosedDay | None


class StartupError(RuntimeError):
    pass


async def build(
    settings: Settings,
    base: BaseConfig,
    cfg: MiniBotConfig,
    *,
    sink: ParquetSink,
    http: httpx.AsyncClient,
    reporter: Reporter,
) -> Parts:
    pm = base.polymarket
    rec = cfg.recorder_view()
    gamma = GammaClient(http, pm.gamma_url, sink)
    discovery = Discovery(gamma, rec, sink)
    try:
        await discovery.resolve_tags()
    except (GammaError, httpx.HTTPError, json.JSONDecodeError) as exc:
        raise StartupError(
            f"league tags: {exc!r}; check the slugs with `polybot minibot --dry-run`"
        ) from exc
    pool = MarketPool(rec.market_ws, pm.market_ws_url, sink, BookTracker())
    venue = PaperVenue(
        cash=Decimal(str(cfg.risk.deposit_usd)),
        latency_ns=int(cfg.paper.latency_ms * 1_000_000),
        clock=now_ns,
    )
    engine = Engine(
        cfg,
        venue=venue,
        pool=pool,
        sink=sink,
        reporter=reporter,
        fetch_market=gamma.get_market,
        state_dir=settings.data_dir / "state",
    )
    restored = engine.state_path.exists()
    try:
        late_day = engine.load_state()
    except RuntimeError as exc:
        raise StartupError(str(exc)) from exc
    engine.drop_all()  # CLAUDE.md, rule 9: start from no open orders
    if late_day is not None:
        engine.save_state()  # the new day is on disk: a quick restart does not report it twice
    pool.listeners.append(engine.on_ws_event)
    return Parts(discovery, pool, engine, reporter, restored, late_day)


async def _run(
    settings: Settings,
    base: BaseConfig,
    cfg: MiniBotConfig,
    sink: ParquetSink,
    http: httpx.AsyncClient,
    *,
    stop: asyncio.Event | None = None,
) -> int:
    """The mini-bot until `stop` (signals when not given); `stop` and `http` are test seams."""
    reporter = Reporter(make_notifier(settings, cfg, http), cfg, settings.data_dir / REPORTS_DIR)
    alerts = AlertThrottle(settings.data_dir / "state" / ALERTS_FILE)
    geo = await check_geoblock(http, base.geoblock, sink)
    if not geo.allowed:
        log.critical("geoblock_start_refused", verdict=geo.verdict.value, detail=geo.detail)
        if alerts.allow("geoblock", REFUSAL_ALERT_EVERY_S):
            await reporter.geoblock(geo.verdict.value)
        return EXIT_GEOBLOCK
    try:
        parts = await build(settings, base, cfg, sink=sink, http=http, reporter=reporter)
    except StartupError as exc:
        log.critical("minibot_startup_failed", error=str(exc))
        if alerts.allow("startup_failed", REFUSAL_ALERT_EVERY_S):
            await reporter.startup_failed(str(exc))
        return EXIT_STARTUP
    engine, pool = parts.engine, parts.pool

    def extra() -> dict[str, Any]:
        return {
            "memory": process_memory(),
            "market_ws": pool.health(),
            "sink": sink.stats.as_dict(),
        }

    if stop is None:
        stop = asyncio.Event()
        install_signal_handlers(stop)
    stop_event = stop
    geo_stopped = False

    async def on_geo_violation(status: GeoStatus) -> None:
        nonlocal geo_stopped
        geo_stopped = True
        engine.drop_all()
        await reporter.geoblock(status.verdict.value)
        stop_event.set()

    tasks: dict[str, CoroutineFactory] = {
        "selection": lambda: selection_loop(parts.discovery, engine, pool, reporter, cfg),
        "snapshot_watch": pool.run_snapshot_watch,
        "engine": engine.run,
        "settlement": engine.settlement_loop,
        "status": lambda: status_loop(engine, reporter, extra, sink, cfg),
        "housekeeping": lambda: housekeeping_loop(settings.data_dir / RAW_DIR, cfg.keep_raw_days),
        "geoguard": GeoGuard(http, base.geoblock, on_geo_violation, sink).run,
    }
    status = engine.write_status(extra())
    log.info(
        "minibot_start",
        leagues=list(cfg.leagues),
        value=round(status.value, 2),
        restored=parts.restored,
        country=geo.country,
    )
    if parts.late_day is not None:
        await reporter.day_closed(parts.late_day)
    if alerts.allow("start", LIFECYCLE_ALERT_EVERY_S):
        await reporter.started(status, restored=parts.restored)
    try:
        clean = await supervise(tasks, stop_event)
    finally:
        engine.drop_all()
        engine.save_state()
        status = engine.write_status(extra())
        await pool.close()
        await engine.wait_reports()
    if geo_stopped:
        return EXIT_GEOBLOCK
    if clean:
        await reporter.stopped(status, "команда остановки")
    elif alerts.allow("crash", LIFECYCLE_ALERT_EVERY_S):
        await reporter.stopped(status, "сбой компонента, Docker перезапустит; см. логи")
    return EXIT_OK if clean else EXIT_CRASH


# ---------------------------------------------------------------------- dry run


async def dry_run(base: BaseConfig, cfg: MiniBotConfig, *, max_pages: int = 10) -> tuple[str, int]:
    """Check league slugs, market types and outcomes against Gamma; preview the selection."""
    out = ["# Мини-бот: проверка конфигурации (--dry-run)", ""]
    ok = True
    async with make_client(base.http) as http:
        geo = await check_geoblock(http, base.geoblock)
        out.append(f"Geoblock: **{geo.verdict.value}** (страна {geo.country}; {geo.detail}).")
        gamma = GammaClient(http, base.polymarket.gamma_url, sink=None)
        league_ids: dict[str, int | None] = {}
        for slug in cfg.leagues:
            try:
                league_ids[slug] = await gamma.resolve_tag_id(slug)
            except (GammaError, httpx.HTTPError, json.JSONDecodeError):
                league_ids[slug] = None
        issues = ParseIssues()
        events: dict[str, PmEvent] = {}
        per_league: Counter[str] = Counter()
        for slug, tag_id in league_ids.items():
            if tag_id is None:
                continue
            async for page in gamma.iter_events(tag_id=tag_id, page_size=50, max_pages=max_pages):
                for raw in page:
                    event = parse_event(raw, "soccer", issues)
                    if event.is_match:
                        per_league[slug] += 1
                        events.setdefault(event.event_id, event)
        rows = []
        for slug, tag_id in league_ids.items():
            found = tag_id is not None
            ok = ok and found
            rows.append(
                [slug, league_name(slug), tag_id if found else "**не найден**", per_league[slug]]
            )
        out += [
            "",
            "## Лиги (`config/minibot.yaml` → `leagues`)",
            "",
            md_table(["slug", "лига", "tag id", "открытых матчей"], rows),
        ]
        out += await _soccer_tags(gamma, max_pages)
    out += _market_types(events.values())
    now = now_ns()
    pairs = recordable_markets(
        events.values(),
        market_types={"soccer": cfg.market_types},
        exclude_doubles={},
        now_ns=now,
        horizon_ns=int(cfg.selection.horizon_h * 3600 * NS_PER_S),
        lookback_ns=0,
    )
    result = select(pairs, cfg, now, paper=True)
    out += _selection(result, cfg)
    if not ok:
        out += [
            "",
            "Есть ненайденные slug-и: мини-бот с таким конфигом не стартует. Подберите slug",
            "по таблице тегов выше и исправьте `leagues` в `config/minibot.yaml`.",
        ]
    return "\n".join(out) + "\n", 0 if ok else EXIT_STARTUP


async def _soccer_tags(gamma: GammaClient, max_pages: int) -> list[str]:
    """Tag frequency on soccer match events: where the league slugs come from."""
    out = ["", "## Теги футбольных матчей (тег `soccer`)", ""]
    try:
        soccer_id = await gamma.resolve_tag_id("soccer")
    except (GammaError, httpx.HTTPError, json.JSONDecodeError) as exc:
        return [*out, f"Тег `soccer` не найден ({type(exc).__name__}): частоту тегов не посчитать."]
    counts: Counter[str] = Counter()
    matches = 0
    issues = ParseIssues()
    async for page in gamma.iter_events(tag_id=soccer_id, page_size=50, max_pages=max_pages):
        for raw in page:
            event = parse_event(raw, "soccer", issues)
            if event.is_match:
                matches += 1
                counts.update(set(event.tag_slugs))
    rows = [[slug, n] for slug, n in counts.most_common(40)]
    return [
        *out,
        f"Матчей просмотрено: {matches} (первые {max_pages} страниц по 50 событий).",
        "",
        md_table(["slug тега", "матчей"], rows),
    ]


def _market_types(events: Iterable[PmEvent]) -> list[str]:
    types: Counter[str] = Counter()
    outcomes: dict[str, str] = {}
    for event in events:
        for market in event.markets:
            kind = market.sports_market_type or "∅"
            types[kind] += 1
            outcomes.setdefault(kind, " / ".join(market.outcomes))
    rows = [[kind, n, outcomes.get(kind, "")] for kind, n in types.most_common()]
    return [
        "",
        "## Типы рынков в матчах лиг (`market_types`)",
        "",
        md_table(["sportsMarketType", "рынков", "исходы (пример)"], rows)
        if rows
        else "Матчей нет.",
    ]


def _selection(result: SelectionResult, cfg: MiniBotConfig) -> list[str]:
    rows = []
    for cand in result.chosen:
        rewards = cand.rewards
        rows.append(
            [
                league_name(cand.league),
                cand.title,
                cand.label,
                cand.market.game_start_raw,
                f"{rewards.daily_rate:g}" if rewards else "—",
                f"{rewards.max_spread:g}" if rewards else "—",
                f"{rewards.min_size:g}" if rewards else "—",
                cand.market.tick_size,
                f"`{cand.template_id}`" + ("" if cand.reviewed else " ⚠️"),
            ]
        )
    out = [
        "",
        "## Отбор сейчас",
        "",
        f"Подходят {result.eligible}, выбрано {len(result.chosen)} (не больше"
        f" {cfg.selection.max_markets}). Пропущено: {dict(result.skipped) or '—'}.",
        "",
    ]
    if rows:
        out.append(
            md_table(
                [
                    "лига",
                    "матч",
                    "рынок",
                    "старт (UTC)",
                    "награды $/день",
                    "v (цена)",
                    "мин. размер",
                    "тик",
                    "правила",
                ],
                rows,
            )
        )
    approved = set(cfg.rules.approved_templates)
    out += ["", "## Шаблоны правил резолюции", ""]
    for template_id, template in sorted(result.templates.items()):
        state = "одобрен" if template_id in approved else "не одобрен"
        out.append(f"- `{template_id}` ({state}): пример — {template.example_title}")
    if not result.templates:
        out.append("—")
    out += [
        "",
        "v — `rewardsMaxSpread`, пересчитанный в цену по `quoting.rewards_spread_unit`",
        "(единица не проверена, docs/api_notes.md §9). ⚠️ — правила не одобрены: на бумаге",
        "котируются, если `rules.paper_quote_unreviewed: true`.",
    ]
    return out


def read_status(path: Path) -> Status | None:
    try:
        return Status.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError, KeyError):
        return None
