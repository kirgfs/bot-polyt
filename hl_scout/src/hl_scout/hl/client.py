"""Async client for the public Hyperliquid Info API. Read-only: no keys, no orders.

Every request type, body and limit used here is listed with its source in docs/api_notes.md.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from hl_scout.config import ApiCfg
from hl_scout.hl.ratelimit import WeightLimiter
from hl_scout.log import get_logger

log = get_logger(__name__)

# [api_notes §4]
BASE_WEIGHT: dict[str, int] = {
    "l2Book": 2,
    "allMids": 2,
    "clearinghouseState": 2,
    "orderStatus": 2,
    "spotClearinghouseState": 2,
    "exchangeStatus": 2,
    "userRole": 60,
}
DEFAULT_WEIGHT = 20
# +1 weight per N items in the response
ITEMS_PER_WEIGHT: dict[str, int] = {
    "userFills": 20,
    "userFillsByTime": 20,
    "fundingHistory": 20,
    "userFunding": 20,
    "userNonFundingLedgerUpdates": 20,  # listed as "nonUserFundingUpdates" in docs; charged conservatively
    "historicalOrders": 20,
    "recentTrades": 20,
    "candleSnapshot": 60,
}

INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


class HyperliquidError(RuntimeError):
    """Non-retryable API error (4xx other than 429, malformed payload)."""


class ConnectivityError(HyperliquidError):
    """The API is not reachable at all: stop the run instead of retrying every request for an hour."""


def describe_transport_error(exc: BaseException) -> str:
    """Human-readable reason for a network-level failure (Russian, for the console)."""
    text = str(exc)
    if isinstance(exc, httpx.ProxyError):
        if "403" in text:
            return "сеть или прокси запрещает доступ к хосту (HTTP 403 на CONNECT)"
        return f"ошибка прокси: {text}"
    if isinstance(exc, httpx.ConnectTimeout | httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout):
        return "таймаут соединения"
    if isinstance(exc, httpx.ConnectError):
        return f"не удаётся подключиться (DNS, файрвол или нет интернета): {text}"
    return text or exc.__class__.__name__


@dataclass(frozen=True)
class FillsResult:
    fills: list[dict[str, Any]]
    truncated: bool  # the 10 000-most-recent limit may have cut the requested range [api_notes §3]
    pages: int


class InfoClient:
    def __init__(
        self,
        cfg: ApiCfg,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        limiter: WeightLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.cfg = cfg
        self._http = httpx.AsyncClient(
            base_url=cfg.base_url,
            timeout=cfg.timeout_s,
            transport=transport,
            headers={"Content-Type": "application/json", "User-Agent": "hl-scout/0.1 (read-only)"},
        )
        self.limiter = limiter or WeightLimiter(cfg.weight_budget_per_min)
        self._sem = asyncio.Semaphore(cfg.max_concurrency)
        self._sleep = sleep
        self.requests = 0
        # circuit breaker: after N network errors in a row the API counts as down. Calls fail fast; after a
        # cooldown one probe is let through (a long-running monitor recovers on its own).
        self._consecutive_failures = 0
        self._tripped_at: float | None = None
        self.breaker_cooldown_s = 60.0

    async def __aenter__(self) -> InfoClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- transport ----------------------------------------------------------------

    def _backoff(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(self.cfg.backoff_max_s, max(retry_after, 1.0))
        base = self.cfg.backoff_base_s * (2**attempt)
        return min(self.cfg.backoff_max_s, base) * (0.5 + random.random() / 2)

    def _breaker_check(self) -> None:
        if self._consecutive_failures < self.cfg.max_consecutive_failures:
            return
        now = time.monotonic()
        if self._tripped_at is None:
            self._tripped_at = now
        elif now - self._tripped_at >= self.breaker_cooldown_s:
            self._tripped_at = now  # half-open: let one probe through
            self._consecutive_failures = self.cfg.max_consecutive_failures - 1
            return
        raise ConnectivityError(f"{self._consecutive_failures} сетевых ошибок подряд — API недоступен")

    async def _request(self, method: str, url: str, *, json: Any = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.cfg.retries + 1):
            self._breaker_check()
            try:
                async with self._sem:
                    self.requests += 1
                    resp = await self._http.request(method, url, json=json)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                self._consecutive_failures += 1
                self._breaker_check()
                delay = self._backoff(attempt)
                log.warning(
                    "hl_transport_retry",
                    url=url,
                    attempt=attempt,
                    delay_s=round(delay, 2),
                    err=describe_transport_error(exc),
                )
                await self._sleep(delay)
                continue
            self._consecutive_failures = 0
            self._tripped_at = None
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("retry-after")
                ra = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else None
                if resp.status_code == 429:
                    ra = max(ra or 0.0, 10.0 * (attempt + 1))
                delay = self._backoff(attempt, ra)
                last_error = HyperliquidError(f"HTTP {resp.status_code}")
                log.warning("hl_http_retry", url=url, status=resp.status_code, attempt=attempt, delay_s=round(delay, 2))
                if resp.status_code == 429:
                    self.limiter.charge(self.limiter.capacity / 4)  # back off the whole budget, not just this call
                await self._sleep(delay)
                continue
            if resp.status_code >= 400:
                raise HyperliquidError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                return resp.json()
            except ValueError as exc:
                raise HyperliquidError(f"не JSON от {url}: {resp.text[:200]}") from exc
        raise HyperliquidError(f"{url}: исчерпаны повторы ({last_error})")

    def _max_items(self, rtype: str) -> int:
        """Largest response of a paginated request type [api_notes §3] (reserved before the call)."""
        if rtype in ("userFills", "userFillsByTime"):
            return self.cfg.fills_page_max
        if rtype == "candleSnapshot":
            return self.cfg.candles_available_max
        return self.cfg.range_page_max

    async def info(self, payload: dict[str, Any]) -> Any:
        rtype = str(payload["type"])
        per = ITEMS_PER_WEIGHT.get(rtype)
        reserve = self._max_items(rtype) // per if per else 0
        ticket = await self.limiter.acquire(BASE_WEIGHT.get(rtype, DEFAULT_WEIGHT), reserve=reserve)
        data: Any = None
        try:
            data = await self._request("POST", "/info", json=payload)
            return data
        finally:
            extra = len(data) // per if per and isinstance(data, list) else 0
            self.limiter.settle(ticket, reserve, extra)

    async def get_json(self, url: str) -> Any:
        """Plain GET for the stats host (leaderboard). Not part of the Info weight budget."""
        return await self._request("GET", url)

    async def preflight(self, timeout_s: float = 15.0) -> int:
        """One cheap request (allMids, weight 2) before any real work, without the retry loop.

        Returns the number of priced coins; raises ConnectivityError with a readable reason otherwise."""
        await self.limiter.acquire(BASE_WEIGHT["allMids"])
        try:
            resp = await self._http.post("/info", json={"type": "allMids"}, timeout=timeout_s)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise ConnectivityError(f"{self.cfg.base_url}: {describe_transport_error(exc)}") from exc
        if resp.status_code != 200:
            raise ConnectivityError(f"{self.cfg.base_url}: HTTP {resp.status_code} {resp.text[:120]}")
        try:
            mids = resp.json()
        except ValueError as exc:
            raise ConnectivityError(f"{self.cfg.base_url}: ответ не JSON") from exc
        if not isinstance(mids, dict) or not mids:
            raise ConnectivityError(f"{self.cfg.base_url}: пустой ответ allMids")
        return len(mids)

    # --- simple requests [api_notes §2] ---------------------------------------------

    async def all_mids(self) -> dict[str, str]:
        return await self.info({"type": "allMids", "dex": ""})

    async def extra_agents(self, user: str) -> Any:
        return await self.info({"type": "extraAgents", "user": user})

    async def meta_and_asset_ctxs(self) -> Any:
        return await self.info({"type": "metaAndAssetCtxs"})

    async def spot_meta_and_asset_ctxs(self) -> Any:
        return await self.info({"type": "spotMetaAndAssetCtxs"})

    async def clearinghouse_state(self, user: str) -> Any:
        return await self.info({"type": "clearinghouseState", "user": user, "dex": ""})

    async def spot_clearinghouse_state(self, user: str) -> Any:
        return await self.info({"type": "spotClearinghouseState", "user": user})

    async def portfolio(self, user: str) -> Any:
        return await self.info({"type": "portfolio", "user": user})

    async def user_role(self, user: str) -> Any:
        return await self.info({"type": "userRole", "user": user})

    async def sub_accounts(self, user: str) -> list[dict[str, Any]]:
        """Sub-accounts of a master: name, subAccountUser, clearinghouseState, spotState [api_notes §2]."""
        return await self.info({"type": "subAccounts", "user": user}) or []

    async def user_fills(self, user: str, aggregate: bool = True) -> list[dict[str, Any]]:
        """At most 2000 most recent fills, newest first [api_notes §3]."""
        return await self.info({"type": "userFills", "user": user, "aggregateByTime": aggregate}) or []

    # --- paginated requests [api_notes §3] --------------------------------------------

    async def user_fills_by_time(
        self, user: str, start_ms: int, end_ms: int, *, aggregate: bool = True, max_pages: int | None = None
    ) -> FillsResult:
        page_max = self.cfg.fills_page_max
        max_pages = max_pages or math.ceil(self.cfg.fills_available_max / page_max) + 1
        seen: dict[tuple[Any, ...], dict[str, Any]] = {}
        cursor, pages, hit_page_cap = start_ms, 0, False
        while cursor <= end_ms:
            if pages >= max_pages:
                hit_page_cap = True
                break
            page = await self.info(
                {
                    "type": "userFillsByTime",
                    "user": user,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "aggregateByTime": aggregate,
                }
            )
            pages += 1
            if not isinstance(page, list) or not page:
                break
            for f in page:
                seen[(f.get("tid"), f.get("oid"), f.get("time"), f.get("px"), f.get("sz"))] = f
            if len(page) < page_max:
                break
            last = max(int(f["time"]) for f in page)
            cursor = last if last > cursor else cursor + 1
        fills = sorted(seen.values(), key=lambda f: (int(f["time"]), int(f.get("tid") or 0)))
        truncated = hit_page_cap or len(fills) >= self.cfg.fills_available_max - page_max // 4
        return FillsResult(fills=fills, truncated=truncated, pages=pages)

    async def _paged_by_time(
        self,
        body: dict[str, Any],
        start_ms: int,
        end_ms: int,
        key: Callable[[dict[str, Any]], tuple[Any, ...]],
        max_pages: int = 400,
    ) -> list[dict[str, Any]]:
        seen: dict[tuple[Any, ...], dict[str, Any]] = {}
        cursor, pages = start_ms, 0
        while cursor <= end_ms and pages < max_pages:
            page = await self.info({**body, "startTime": cursor, "endTime": end_ms})
            pages += 1
            if not isinstance(page, list) or not page:
                break
            for item in page:
                seen[key(item)] = item
            if len(page) < self.cfg.range_page_max:
                break
            last = max(int(item["time"]) for item in page)
            cursor = last if last > cursor else cursor + 1
        return sorted(seen.values(), key=lambda x: int(x["time"]))

    async def funding_history(self, coin: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return await self._paged_by_time(
            {"type": "fundingHistory", "coin": coin}, start_ms, end_ms, key=lambda x: (x["time"],)
        )

    async def user_funding(self, user: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return await self._paged_by_time(
            {"type": "userFunding", "user": user},
            start_ms,
            end_ms,
            key=lambda x: (x["time"], x.get("hash"), (x.get("delta") or {}).get("coin")),
        )

    async def ledger_updates(self, user: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return await self._paged_by_time(
            {"type": "userNonFundingLedgerUpdates", "user": user},
            start_ms,
            end_ms,
            key=lambda x: (x["time"], x.get("hash"), (x.get("delta") or {}).get("type")),
        )

    async def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        step = INTERVAL_MS[interval]
        # only the most recent 5000 candles exist [api_notes §3]; do not ask for more
        start_ms = max(start_ms, end_ms - step * self.cfg.candles_available_max)
        seen: dict[int, dict[str, Any]] = {}
        cursor = start_ms
        for _ in range(10):
            page = await self.info(
                {
                    "type": "candleSnapshot",
                    "req": {"coin": coin, "interval": interval, "startTime": cursor, "endTime": end_ms},
                }
            )
            if not isinstance(page, list) or not page:
                break
            for c in page:
                seen[int(c["t"])] = c
            last = max(int(c["t"]) for c in page)
            if len(page) < self.cfg.candles_available_max or last + step > end_ms or last < cursor:
                break
            cursor = last + step
        return [seen[t] for t in sorted(seen)]


async def gather_limited(coros: Iterable[Awaitable[Any]], limit: int) -> list[Any]:
    """Run awaitables with bounded concurrency. Per-item errors are returned (one bad address must not stop
    discovery), but a ConnectivityError — the API itself is down — is raised: continuing would be pointless."""
    sem = asyncio.Semaphore(limit)

    async def run(c: Awaitable[Any]) -> Any:
        async with sem:
            try:
                return await c
            except Exception as exc:  # caller decides
                return exc

    results = await asyncio.gather(*(run(c) for c in coros))
    fatal = next((r for r in results if isinstance(r, ConnectivityError)), None)
    if fatal is not None:
        raise fatal
    return results
