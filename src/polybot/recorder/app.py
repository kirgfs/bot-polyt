"""Recorder orchestration (M1): geoblock gate, then all feeds into Parquet until stopped.

Exit codes: 0 stopped by signal, 1 a component died, 2 geoblock not allowed (at start or
later), 3 configuration or startup discovery failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import httpx

from polybot.core.config import AppConfig, Settings
from polybot.core.http import make_client
from polybot.core.logging import get_logger
from polybot.core.timeutil import now_ns
from polybot.data.records import Kind, Record, Source
from polybot.data.sink import ParquetSink
from polybot.feeds.odds.oddspapi import (
    OddsPapiClient,
    OddsPapiConfigError,
    OddsPapiWsRecorder,
    RequestBudget,
)
from polybot.recorder.discovery import Discovery
from polybot.recorder.health import Health
from polybot.recorder.oddspapi_task import OddsPapiPaidRest
from polybot.recorder.rest_tasks import ClobMetaCollector, RestProbe, RestValidator
from polybot.venues.polymarket.clob_rest import ClobPublic
from polybot.venues.polymarket.clob_ws import MarketPool
from polybot.venues.polymarket.gamma import GammaClient, GammaError
from polybot.venues.polymarket.geoblock import GeoGuard, GeoStatus, check_geoblock
from polybot.venues.polymarket.orderbook import BookTracker
from polybot.venues.polymarket.sports_ws import SportsFeed

log = get_logger(__name__)

EXIT_OK = 0
EXIT_CRASH = 1
EXIT_GEOBLOCK = 2
EXIT_STARTUP = 3

# Consecutive failed discovery polls before a critical log. Recording goes on with the
# last known subscriptions; the report shows the gap.
DISCOVERY_ALERT_AFTER = 5

CoroutineFactory = Callable[[], Coroutine[Any, Any, Any]]


class StartupError(RuntimeError):
    pass


def lifecycle_record(sink: ParquetSink, event: str, **info: object) -> None:
    sink.write(
        Record(
            ts_recv_ns=now_ns(),
            source=Source.RECORDER,
            kind=Kind.CONTROL,
            event_type=event,
            payload=json.dumps(info, default=str),
        )
    )


async def discovery_loop(
    discovery: Discovery, pool: MarketPool, meta: ClobMetaCollector, interval_s: float
) -> None:
    failures = 0
    while True:
        try:
            result = await discovery.poll()
        except (GammaError, httpx.HTTPError, json.JSONDecodeError) as exc:
            failures += 1
            discovery.stats.errors += 1
            report = log.critical if failures >= DISCOVERY_ALERT_AFTER else log.warning
            report("discovery_poll_failed", error=repr(exc), consecutive=failures)
        else:
            failures = 0
            await pool.set_assets(result.token_ids)
            meta.condition_ids = result.condition_ids
        await asyncio.sleep(interval_s)


@dataclass
class Components:
    pool: MarketPool
    tasks: dict[str, CoroutineFactory] = field(default_factory=dict)
    health: dict[str, Callable[[], dict[str, object]]] = field(default_factory=dict)


async def build_components(
    settings: Settings, cfg: AppConfig, sink: ParquetSink, http: httpx.AsyncClient
) -> Components:
    rec, pm = cfg.recorder, cfg.base.polymarket
    gamma = GammaClient(http, pm.gamma_url, sink)
    discovery = Discovery(gamma, rec, sink)
    try:
        await discovery.resolve_tags()
    except (GammaError, httpx.HTTPError, json.JSONDecodeError) as exc:
        raise StartupError(f"Gamma tag resolution failed: {exc!r}") from exc

    pool = MarketPool(rec.market_ws, pm.market_ws_url, sink, BookTracker())
    clob = ClobPublic(http, pm.clob_url)
    meta = ClobMetaCollector(clob, rec.clob_meta, sink)
    validator = RestValidator(clob, pool, rec.rest_validation, sink)
    probe = RestProbe(clob, rec.probes, sink)
    parts = Components(pool=pool)
    parts.tasks = {
        "discovery": lambda: discovery_loop(discovery, pool, meta, rec.discovery.interval_s),
        "snapshot_watch": pool.run_snapshot_watch,
        "clob_meta": meta.run,
        "rest_probe": probe.run,
    }
    parts.health = {
        "market_ws": pool.health,
        "discovery": lambda: {
            "polls": discovery.stats.polls,
            "errors": discovery.stats.errors,
            "counts": discovery.stats.last_counts,
            "issues": discovery.stats.last_issues,
        },
        "rest_validation": lambda: dict(validator.stats.__dict__),
        "rest_probe_ms": lambda: {"last": probe.last_latency_ms},
    }
    if rec.rest_validation.enabled:
        parts.tasks["rest_validation"] = validator.run
    if rec.sports_ws.enabled:
        sports = SportsFeed(rec.sports_ws, pm.sports_ws_url, sink)
        parts.tasks["sports_ws"] = sports.run
        parts.health["sports_ws"] = sports.health
    try:
        odds = make_oddspapi_task(settings, cfg, sink, http, discovery)
    except OddsPapiConfigError as exc:
        raise StartupError(str(exc)) from exc
    if odds is not None:
        parts.tasks["oddspapi"] = odds
    return parts


def make_oddspapi_task(
    settings: Settings,
    cfg: AppConfig,
    sink: ParquetSink,
    http: httpx.AsyncClient,
    discovery: Discovery,
) -> CoroutineFactory | None:
    odds_cfg = cfg.recorder.oddspapi
    if odds_cfg.mode == "off":
        return None
    if odds_cfg.mode == "ws":
        return OddsPapiWsRecorder(odds_cfg, settings.oddspapi_api_key, sink).run
    budget = RequestBudget(
        settings.data_dir / "state" / "oddspapi_budget.json",
        odds_cfg.monthly_request_budget,
        odds_cfg.daily_request_budget,
    )
    client = OddsPapiClient(http, odds_cfg, settings.oddspapi_api_key, sink, budget)
    return OddsPapiPaidRest(client, odds_cfg, discovery, cfg.recorder.sports).run


def install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not available on Windows (the development machine); Ctrl+C still works there.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)


async def supervise(tasks: dict[str, CoroutineFactory], stop: asyncio.Event) -> bool:
    """Run until `stop` is set (True) or any task finishes on its own (False)."""
    running = {asyncio.create_task(factory(), name=name): name for name, factory in tasks.items()}
    waiter = asyncio.create_task(stop.wait(), name="stop")
    clean = True
    try:
        done, _ = await asyncio.wait([waiter, *running], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            error = None if task.cancelled() else task.exception()
            if task is waiter or (stop.is_set() and error is None):
                continue  # requested stop (signal or GeoGuard), not a crash
            log.critical("component_stopped", component=running[task], error=repr(error))
            clean = False
    finally:
        everything: list[asyncio.Task[Any]] = [waiter, *running]
        for task in everything:
            task.cancel()
        await asyncio.gather(*everything, return_exceptions=True)
    return clean


async def run_recorder(settings: Settings, cfg: AppConfig) -> int:
    rec = cfg.recorder
    sink = ParquetSink(
        settings.data_dir / "raw",
        flush_interval_s=rec.sink.flush_interval_s,
        flush_rows=rec.sink.flush_rows,
        flush_mb=rec.sink.flush_mb,
        max_buffer_mb=rec.sink.max_buffer_mb,
        max_buffer_rows=rec.sink.max_buffer_rows,
        compression_level=rec.sink.compression_level,
    )
    sink_task = asyncio.create_task(sink.run(), name="sink")
    exit_code = EXIT_OK
    try:
        async with make_client(cfg.base.http) as http:
            geo = await check_geoblock(http, cfg.base.geoblock, sink)
            if not geo.allowed:
                log.critical("geoblock_start_refused", verdict=geo.verdict.value, detail=geo.detail)
                lifecycle_record(
                    sink, "start_refused", verdict=geo.verdict.value, detail=geo.detail
                )
                exit_code = EXIT_GEOBLOCK
                return exit_code
            lifecycle_record(sink, "recorder_start", country=geo.country, run_id=sink.run_id)
            try:
                parts = await build_components(settings, cfg, sink, http)
            except StartupError as exc:
                log.critical("recorder_startup_failed", error=str(exc))
                exit_code = EXIT_STARTUP
                return exit_code

            stop = asyncio.Event()
            geo_stopped = False

            async def on_geo_violation(status: GeoStatus) -> None:
                nonlocal geo_stopped
                geo_stopped = True
                lifecycle_record(sink, "geoblock_stop", verdict=status.verdict.value)
                stop.set()

            health = Health(settings.data_dir / "state", rec.health, sink, parts.health)
            parts.tasks["health"] = health.run
            parts.tasks["geoguard"] = GeoGuard(http, cfg.base.geoblock, on_geo_violation, sink).run
            install_signal_handlers(stop)
            try:
                clean = await supervise(parts.tasks, stop)
            finally:
                await parts.pool.close()
            exit_code = EXIT_GEOBLOCK if geo_stopped else (EXIT_OK if clean else EXIT_CRASH)
            lifecycle_record(sink, "recorder_stop", exit_code=exit_code)
    finally:
        sink_task.cancel()
        await asyncio.gather(sink_task, return_exceptions=True)
        await sink.close()
        log.info("recorder_stopped", exit_code=exit_code, sink=sink.stats.as_dict())
    return exit_code
