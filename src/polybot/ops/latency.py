"""VPS → Polymarket latency: REST (warm/cold), TCP to the edge, WS PING→PONG, WS one-way.

Output is a Markdown table for docs/latency.md (methodology there). One-way WS delay uses
the server `timestamp` of events, so it is only meaningful with chrony-synced clocks;
the tool reports the local clock state next to the numbers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect as ws_connect

from polybot.analytics.stats import Summary, md_table, summarize
from polybot.core.config import AppConfig
from polybot.core.http import make_client
from polybot.core.timeutil import now_ns
from polybot.venues.polymarket.clob_rest import cf_colo
from polybot.venues.polymarket.clob_ws import subscribe_initial
from polybot.venues.polymarket.gamma import GammaClient
from polybot.venues.polymarket.markets import ParseIssues, parse_event

# Events whose `timestamp` is the send time of a change; `book` snapshots after subscribe
# may carry an older time and would inflate the one-way estimate.
_ONE_WAY_EVENTS = frozenset(
    {"price_change", "last_trade_price", "best_bid_ask", "tick_size_change"}
)


@dataclass
class LatencyReport:
    rest_warm_ms: Summary
    rest_cold_ms: Summary
    tcp_ms: Summary
    book_ms: Summary
    ws_rtt_ms: Summary
    ws_one_way_ms: Summary
    ws_events: int
    server_offset_s: float | None
    chrony: dict[str, str]
    colos: Counter[str] = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)


def chrony_tracking() -> dict[str, str]:
    binary = shutil.which("chronyc")
    if binary is None:
        return {"status": "chronyc not found here (run `chronyc tracking` on the VPS host)"}
    try:
        output = subprocess.run(  # noqa: S603 - fixed binary path and arguments
            [binary, "tracking"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": f"chronyc failed: {exc!r}"}
    result: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in ("System time", "Last offset", "RMS offset", "Leap status"):
            result[key.strip()] = value.strip()
    return result or {"status": "no chronyc output"}


async def _active_tokens(cfg: AppConfig, http: httpx.AsyncClient, limit: int) -> list[str]:
    """Tokens of the soonest upcoming recordable markets, for the WS part."""
    gamma = GammaClient(http, cfg.base.polymarket.gamma_url, sink=None)
    now = now_ns()
    candidates: list[tuple[int, str]] = []
    for sport, sport_cfg in cfg.recorder.sports.items():
        tag_id = await gamma.resolve_tag_id(sport_cfg.tag_slugs[0])
        page = await gamma.list_events(tag_id=tag_id, page_size=100, max_pages=3)
        for raw in page.events:
            event = parse_event(raw, sport, ParseIssues())
            for market in event.markets:
                start = market.game_start_ns
                if (
                    market.sports_market_type in sport_cfg.market_types
                    and market.is_binary_with_tokens
                    and start is not None
                    and start > now
                ):
                    candidates.extend((start, token) for token in market.token_ids)
    return [token for _, token in sorted(candidates)[:limit]]


async def _rest(
    cfg: AppConfig, http: httpx.AsyncClient, samples: int, cold: int, token: str | None
) -> tuple[list[float], list[float], list[float], list[float], Counter[str], float | None]:
    clob = cfg.base.polymarket.clob_url.rstrip("/")
    warm: list[float] = []
    colos: Counter[str] = Counter()
    offset: float | None = None
    for _ in range(samples):
        started = time.perf_counter()
        response = await http.get(clob + "/time")
        warm.append((time.perf_counter() - started) * 1000)
        colos[cf_colo(response.headers) or "?"] += 1
        with contextlib.suppress(ValueError, json.JSONDecodeError):
            body = response.json()
            server_s = float(body if not isinstance(body, dict) else body.get("time", "nan"))
            offset = server_s - time.time()
        await asyncio.sleep(0.05)
    book: list[float] = []
    if token is not None:
        for _ in range(max(1, samples // 4)):
            started = time.perf_counter()
            await http.get(clob + "/book", params={"token_id": token})
            book.append((time.perf_counter() - started) * 1000)
            await asyncio.sleep(0.05)
    cold_ms: list[float] = []
    tcp_ms: list[float] = []
    host = urlsplit(clob).hostname or ""
    for _ in range(cold):
        started = time.perf_counter()
        async with make_client(cfg.base.http) as fresh:
            await fresh.get(clob + "/time")
        cold_ms.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        _, writer = await asyncio.open_connection(host, 443)
        tcp_ms.append((time.perf_counter() - started) * 1000)
        writer.close()
        await asyncio.sleep(0.2)
    return warm, cold_ms, tcp_ms, book, colos, offset


async def _ws(
    url: str, tokens: list[str], seconds: float, ping_every: float
) -> tuple[list[float], list[float], int]:
    rtt: list[float] = []
    one_way: list[float] = []
    events = 0
    pings: list[float] = []
    async with ws_connect(url, open_timeout=10, max_size=16 * 1024 * 1024) as ws:
        await ws.send(subscribe_initial(tokens, custom=True))

        async def pinger() -> None:
            while True:
                await asyncio.sleep(ping_every)
                pings.append(time.perf_counter())
                await ws.send("PING")

        ping_task = asyncio.create_task(pinger())
        deadline = time.perf_counter() + seconds
        try:
            while (remaining := deadline - time.perf_counter()) > 0:
                try:
                    message = await asyncio.wait_for(ws.recv(), remaining)
                except TimeoutError:
                    break
                received_ms = time.time() * 1000
                text = message.decode() if isinstance(message, bytes) else message
                if text == "PONG":
                    if pings:
                        rtt.append((time.perf_counter() - pings.pop(0)) * 1000)
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    data = json.loads(text)
                    for event in data if isinstance(data, list) else [data]:
                        events += 1
                        stamp = event.get("timestamp") if isinstance(event, dict) else None
                        if event.get("event_type") in _ONE_WAY_EVENTS and stamp:
                            with contextlib.suppress(ValueError):
                                one_way.append(received_ms - int(str(stamp)))
        finally:
            ping_task.cancel()
            await asyncio.gather(ping_task, return_exceptions=True)
    return rtt, one_way, events


async def measure(
    cfg: AppConfig,
    *,
    rest_samples: int = 200,
    cold_samples: int = 20,
    ws_seconds: float = 300.0,
    ws_ping_interval: float = 2.0,
    n_tokens: int = 20,
) -> LatencyReport:
    notes: list[str] = []
    async with make_client(cfg.base.http) as http:
        tokens = await _active_tokens(cfg, http, n_tokens)
        if not tokens:
            notes.append("no upcoming recordable markets found: WS and /book parts skipped")
        warm, cold, tcp, book, colos, offset = await _rest(
            cfg, http, rest_samples, cold_samples, tokens[0] if tokens else None
        )
    rtt: list[float] = []
    one_way: list[float] = []
    events = 0
    if tokens:
        rtt, one_way, events = await _ws(
            cfg.base.polymarket.market_ws_url, tokens, ws_seconds, ws_ping_interval
        )
    if offset is not None and abs(offset) > 2:
        notes.append(f"local clock differs from CLOB /time by {offset:+.1f} s: fix chrony first")
    return LatencyReport(
        rest_warm_ms=summarize(warm),
        rest_cold_ms=summarize(cold),
        tcp_ms=summarize(tcp),
        book_ms=summarize(book),
        ws_rtt_ms=summarize(rtt),
        ws_one_way_ms=summarize(one_way),
        ws_events=events,
        server_offset_s=offset,
        chrony=chrony_tracking(),
        colos=colos,
        notes=notes,
    )


def render(report: LatencyReport) -> str:
    header = ["метрика", "n", "p50, мс", "p95, мс", "p99, мс", "min", "max"]
    rows = [
        ["TCP connect до edge Cloudflare (clob)", *report.tcp_ms.row()],
        ["REST GET /time, тёплое соединение", *report.rest_warm_ms.row()],
        ["REST GET /time, новое соединение (TCP+TLS+запрос)", *report.rest_cold_ms.row()],
        ["REST GET /book, тёплое соединение", *report.book_ms.row()],
        ["WS market: PING→PONG", *report.ws_rtt_ms.row()],
        ["WS market: сервер `timestamp` → получение (one-way)", *report.ws_one_way_ms.row()],
    ]
    colos = ", ".join(f"{k}×{v}" for k, v in report.colos.most_common())
    chrony = "; ".join(f"{k}: {v}" for k, v in report.chrony.items())
    offset = "—" if report.server_offset_s is None else f"{report.server_offset_s:+.2f} с"
    extra = [
        f"- Cloudflare colo (по `cf-ray`): {colos or '—'}",
        f"- Часы: chrony — {chrony}; разница с CLOB `/time` (точность ±1 с) — {offset}",
        f"- WS: событий за замер — {report.ws_events}",
        *(f"- {note}" for note in report.notes),
    ]
    return md_table(header, rows) + "\n\n" + "\n".join(extra)
