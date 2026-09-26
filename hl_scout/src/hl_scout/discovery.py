"""Candidate discovery and data collection into the SQLite cache.

Pool = manual + a seeded random "control" sample of the copyable band of the leaderboard (no selection on profit;
the process check runs on it) + a "search" part (profitable wallets in the band) + large-trade addresses.
Stage 1 fetches only `portfolio` (cheap) and keeps wallets that pass a relaxed screen now or at any walk-forward
fold start; stage 2 fetches everything else (fills, positions, ledger, spot, role) for survivors.
"""

from __future__ import annotations

import random
from typing import Any

import numpy as np

from hl_scout.analytics import build_equity_curve, portfolio_volume
from hl_scout.backtest import make_folds
from hl_scout.config import Config
from hl_scout.hl.client import INTERVAL_MS, HyperliquidError, InfoClient, gather_limited
from hl_scout.hl.ws import collect_large_trades
from hl_scout.log import get_logger
from hl_scout.market import Bars, Funding, MarketData, build_regime, parse_meta, parse_spot_prices
from hl_scout.scoring import WalletData, stage1_pass
from hl_scout.store import Store
from hl_scout.util import DAY, HOUR, MIN, is_address, is_perp_coin, now_ms

log = get_logger(__name__)


def parse_leaderboard(raw: Any) -> list[dict[str, Any]]:
    """Unofficial stats endpoint [api_notes §6]: parse windows by name and tolerate missing fields."""
    rows = raw.get("leaderboardRows") if isinstance(raw, dict) else raw
    out: list[dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        addr = str(r.get("ethAddress") or "").lower()
        if not is_address(addr):
            continue
        perf: dict[str, dict[str, float]] = {}
        for item in r.get("windowPerformances") or []:
            if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict):
                vals = {}
                for k in ("pnl", "roi", "vlm"):
                    try:
                        vals[k] = float(item[1].get(k) or 0.0)
                    except (TypeError, ValueError):
                        vals[k] = 0.0
                perf[str(item[0])] = vals
        try:
            av = float(r.get("accountValue") or 0.0)
        except (TypeError, ValueError):
            av = 0.0
        out.append({"address": addr, "account_value": av, "display_name": r.get("displayName"), "perf": perf})
    return out


def select_pool(
    rows: list[dict[str, Any]], cfg: Config, extra: dict[str, float], manual: list[str]
) -> tuple[list[str], set[str]]:
    """(pool, control addresses). See config.yaml → discovery for the two parts of the pool."""
    d = cfg.discovery

    def p(r: dict[str, Any], w: str, k: str) -> float:
        return float((r["perf"].get(w) or {}).get(k) or 0.0)

    band = [
        r
        for r in rows
        if d.pool_equity_min_usd <= r["account_value"] <= d.pool_equity_max_usd
        and d.pool_month_vlm_min_usd <= p(r, "month", "vlm") <= d.pool_month_vlm_max_usd
    ]
    band.sort(key=lambda r: r["address"])  # deterministic input for the seeded sample
    search = [
        r
        for r in band
        if p(r, "month", "pnl") > 0
        and p(r, "allTime", "pnl") > 0
        and 0 < p(r, "month", "roi") <= d.pool_max_month_roi
        and cfg.filters.equity_min_usd <= r["account_value"] <= cfg.filters.equity_max_usd
    ]
    search.sort(key=lambda r: p(r, "month", "roi"), reverse=True)
    control = random.Random(d.pool_seed).sample(band, min(d.pool_control, len(band)))
    pool: list[str] = []
    seen: set[str] = set()
    # order = priority when pool_max or deep_max cuts: large-trade addresses (mostly whales and market makers,
    # whose trades are too big for a $50 copy) go last
    for a in [
        *manual,
        *(r["address"] for r in control),
        *(r["address"] for r in search[: d.pool_search]),
        *sorted(extra, key=lambda a: -extra[a]),
    ]:
        if a not in seen and len(pool) < d.pool_max:
            pool.append(a)
            seen.add(a)
    return pool, {r["address"] for r in control} & seen


class Discovery:
    def __init__(self, cfg: Config, store: Store, client: InfoClient) -> None:
        self.cfg = cfg
        self.store = store
        self.client = client

    async def run(
        self, *, listen_min: float | None, use_ws: bool, use_lb: bool, extra: list[str] | None = None
    ) -> list[str]:
        """Whole discovery: meta → leaderboard → large trades → pool → stage 1 → stage 2 → market data."""
        cfg, store = self.cfg, self.store
        await self.refresh_meta()
        if extra:
            store.addresses_add(extra, "manual", now_ms())
        if use_lb and cfg.discovery.use_leaderboard:
            await self.refresh_leaderboard()
        if use_ws and cfg.discovery.large_trades.enabled:
            await self.collect_large_trades(listen_min)
        pool = self.pool()
        survivors = await self.stage1(pool)
        survivors += [a for a in extra or [] if a not in survivors]
        done = await self.deep_fetch_all(survivors)
        # every cached wallet is analysed, so market data must be fresh for all of them, not only this run's
        starts = market_starts([load_wallet(store, a) for a in deep_addresses(store)], cfg.universe.allow_hip3)
        starts[cfg.rules.regime.reference_coin] = 0  # the regime needs the whole history
        await self.market_fetch(starts)
        log.info(
            "discovery_done",
            pool=len(pool),
            deep=len(done),
            requests=self.client.requests,
            weight=round(self.client.limiter.spent),
        )
        return done

    # --- exchange metadata --------------------------------------------------------------------------------
    async def refresh_meta(self, force: bool = False) -> None:
        now = now_ms()
        ttl = self.cfg.discovery.ttl.meta_h * HOUR
        if force or self.store.kv_get("meta", "perp", max_age_ms=ttl, now=now) is None:
            self.store.kv_put("meta", "perp", await self.client.meta_and_asset_ctxs(), now)
        if force or self.store.kv_get("meta", "spot", max_age_ms=ttl, now=now) is None:
            self.store.kv_put("meta", "spot", await self.client.spot_meta_and_asset_ctxs(), now)

    # --- leaderboard ----------------------------------------------------------------------------------------
    async def refresh_leaderboard(self) -> int:
        d, now = self.cfg.discovery, now_ms()
        fetched = self.store.leaderboard_fetched_at()
        if fetched is not None and now - fetched < d.ttl.leaderboard_h * HOUR:
            return len(self.store.leaderboard_rows())
        try:
            raw = await self.client.get_json(d.leaderboard_url)
        except Exception as exc:  # the endpoint is unofficial: discovery must survive without it
            log.warning("leaderboard_unavailable", err=str(exc))
            return 0
        rows = parse_leaderboard(raw)
        n = self.store.leaderboard_replace(rows, now)
        log.info("leaderboard_loaded", rows=n)
        return n

    # --- large trades (WebSocket) --------------------------------------------------------------------------
    async def collect_large_trades(self, minutes: float | None = None) -> int:
        lt = self.cfg.discovery.large_trades
        meta = parse_meta(self.store.kv_get("meta", "perp"))
        coins = [c.name for c in sorted(meta.values(), key=lambda m: -m.day_volume) if not c.delisted][: lt.top_coins]
        listen_min = lt.listen_min if minutes is None else minutes
        if not coins or listen_min <= 0:
            return 0
        trades = await collect_large_trades(
            self.cfg.api.ws_url,
            coins,
            lt.min_notional_usd,
            60 * listen_min,
            ping_every_s=self.cfg.api.ws_ping_s,
            pong_timeout_s=self.cfg.api.ws_pong_timeout_s,
        )
        self.store.large_trades_add(trades)
        addrs = {t.buyer for t in trades} | {t.seller for t in trades}
        self.store.addresses_add(sorted(addrs), "large_trade", now_ms())
        return len(addrs)

    # --- pool and stage 1 -------------------------------------------------------------------------------------
    def pool(self) -> list[str]:
        d = self.cfg.discovery
        rows = self.store.leaderboard_rows() if d.use_leaderboard else []
        extra = self.store.large_trade_addresses(now_ms() - 30 * DAY)
        manual = [a.lower() for a in d.manual_addresses if is_address(a)]
        manual += [a for a, src in self.store.addresses().items() if "manual" in src or "followed" in src]
        pool, control = select_pool(rows, self.cfg, extra, manual)
        self.store.addresses_add(sorted(control), "control", now_ms())
        self.store.addresses_add([a for a in pool if a not in control], "pool", now_ms())
        return pool

    async def portfolio(self, addr: str) -> Any:
        now = now_ms()
        ttl = self.cfg.discovery.ttl.portfolio_h * HOUR
        cached = self.store.kv_get("portfolio", addr, max_age_ms=ttl, now=now)
        if cached is not None:
            return cached
        data = await self.client.portfolio(addr)
        self.store.kv_put("portfolio", addr, data, now)
        return data

    async def stage1(self, pool: list[str]) -> list[str]:
        now = now_ms()
        folds = make_folds(now - self.cfg.discovery.history_days * DAY, now, self.cfg)
        checkpoints = [now, *(f.test0 for f in folds)]
        manual = self._manual()

        async def screen(addrs: list[str]) -> dict[str, Any]:
            results = await gather_limited([self.portfolio(a) for a in addrs], self.cfg.api.max_concurrency)
            out: dict[str, Any] = {}
            for addr, res in zip(addrs, results, strict=True):
                if isinstance(res, Exception):
                    log.warning("portfolio_failed", address=addr, err=str(res))
                else:
                    out[addr] = res
            return out

        def passes(addr: str, portfolio: Any) -> bool:
            curve = build_equity_curve(portfolio, prefer="perp")
            total = build_equity_curve(portfolio, prefer="total")
            return addr in manual or any(stage1_pass(curve, total, t, self.cfg) for t in checkpoints)

        portfolios = await screen(pool)
        subs: dict[str, list[str]] = {}
        for addr, pf in portfolios.items():
            if self._trades_through_subaccounts(addr, pf):
                subs[addr] = await self.subaccounts(addr)
        sub_portfolios = await screen([s for ss in subs.values() for s in ss])
        keep: list[str] = []
        for addr in pool:  # a master's sub-accounts keep its place in the priority order
            if addr in portfolios and passes(addr, portfolios[addr]):
                keep.append(addr)
            keep += [s for s in subs.get(addr, []) if s in sub_portfolios and passes(s, sub_portfolios[s])]
        log.info("stage1_done", pool=len(pool), subaccounts=len(sub_portfolios), kept=len(keep))
        return keep[: self.cfg.discovery.deep_max]

    def _trades_through_subaccounts(self, addr: str, portfolio: Any) -> bool:
        """A master's leaderboard row sums its sub-accounts [api_notes §6]: if the address itself traded much less
        than its row says, the trading happens in sub-accounts."""
        sc = self.cfg.discovery.subaccounts
        lb = self.store.leaderboard_row(addr)
        if not sc.expand or lb is None:
            return False
        row_vlm = float((lb["perf"].get("month") or {}).get("vlm") or 0.0)
        return row_vlm > 0 and portfolio_volume(portfolio, "month") < sc.own_volume_ratio * row_vlm

    async def subaccounts(self, master: str) -> list[str]:
        """The biggest sub-accounts of a master (by account value); they inherit the master's pool sources."""
        sc, now = self.cfg.discovery.subaccounts, now_ms()
        ttl = self.cfg.discovery.ttl.role_days * DAY
        raw = self.store.kv_get("subaccounts", master, max_age_ms=ttl, now=now)
        if raw is None:
            try:
                raw = await self.client.sub_accounts(master)
            except HyperliquidError as exc:
                log.warning("subaccounts_failed", master=master, err=str(exc))
                return []
            self.store.kv_put("subaccounts", master, raw, now)
        found: list[tuple[float, str]] = []
        for s in raw or []:
            addr = str(s.get("subAccountUser") or "").lower()
            try:
                av = float(((s.get("clearinghouseState") or {}).get("marginSummary") or {}).get("accountValue") or 0)
            except (TypeError, ValueError):
                av = 0.0
            if is_address(addr) and av >= sc.min_equity_usd:
                found.append((av, addr))
        out = [a for _, a in sorted(found, reverse=True)[: sc.max_per_master]]
        for src in self.store.addresses().get(master, set()) | {"subaccount"}:
            self.store.addresses_add(out, src, now)
        log.info("subaccounts_expanded", master=master, total=len(raw or []), taken=len(out))
        return out

    def _manual(self) -> set[str]:
        return {a for a, src in self.store.addresses().items() if "manual" in src or "followed" in src}

    # --- stage 2: everything about one wallet ----------------------------------------------------------------
    async def deep_fetch(self, addr: str) -> None:
        now = now_ms()
        d = self.cfg.discovery
        start = now - d.history_days * DAY
        cov = self.store.coverage_get("fills", addr)
        fetch_from = start if cov is None or cov[0] > start else max(start, cov[1] - HOUR)
        res = await self.client.user_fills_by_time(addr, fetch_from, now)
        self.store.fills_put(addr, res.fills)
        truncated = res.truncated or (cov is not None and cov[2] and cov[0] <= start)
        self.store.coverage_set("fills", addr, min(start, cov[0]) if cov else start, now, truncated, now)
        lcov = self.store.coverage_get("ledger", addr)
        if lcov is None or now - lcov[1] > d.ttl.ledger_h * HOUR:
            lfrom = start if lcov is None else max(start, lcov[1] - HOUR)
            self.store.ledger_put(addr, await self.client.ledger_updates(addr, lfrom, now))
            self.store.coverage_set("ledger", addr, min(start, lcov[0]) if lcov else start, now, False, now)
        state_ttl = d.ttl.state_min * MIN
        if self.store.kv_get("clearinghouse", addr, max_age_ms=state_ttl, now=now) is None:
            self.store.kv_put("clearinghouse", addr, await self.client.clearinghouse_state(addr), now)
        if self.store.kv_get("spot", addr, max_age_ms=state_ttl, now=now) is None:
            self.store.kv_put("spot", addr, await self.client.spot_clearinghouse_state(addr), now)
        if self.store.kv_get("role", addr, max_age_ms=d.ttl.role_days * DAY, now=now) is None:
            self.store.kv_put("role", addr, await self.client.user_role(addr), now)
        await self.portfolio(addr)

    async def deep_fetch_all(self, addrs: list[str]) -> list[str]:
        done: list[str] = []
        for i, chunk in enumerate(_chunks(addrs, 10)):
            results = await gather_limited([self.deep_fetch(a) for a in chunk], self.cfg.api.max_concurrency)
            for a, r in zip(chunk, results, strict=True):
                if isinstance(r, Exception):
                    log.warning("deep_fetch_failed", address=a, err=str(r))
                else:
                    done.append(a)
            log.info(
                "deep_fetch_progress",
                done=len(done),
                total=len(addrs),
                batch=i + 1,
                weight_spent=round(self.client.limiter.spent),
            )
        return done

    # --- market data ------------------------------------------------------------------------------------------
    async def market_fetch(self, starts: dict[str, int]) -> None:
        """Candles and funding per coin from `starts[coin]` (clamped to the history depth) up to now.

        Market data is needed only from the first trade of any analysed wallet in that coin: fetching every coin
        for the whole history would spend most of the API budget on coins traded once last week."""
        now = now_ms()
        floor = now - self.cfg.discovery.history_days * DAY
        for n, coin in enumerate(sorted(starts), 1):
            start = max(floor, starts[coin])
            for interval in self.cfg.api.candle_intervals:
                step = INTERVAL_MS[interval]
                key = f"{coin}:{interval}"
                cov = self.store.coverage_get("candles", key)
                has_head = cov is not None and cov[0] <= start + step
                if has_head and now - cov[1] < step:
                    continue
                c_from = max(start, cov[1] - 2 * step) if has_head else start
                candles = await self.client.candles(coin, interval, c_from, now)
                self.store.candles_put(coin, interval, candles)
                self.store.coverage_set("candles", key, min(start, cov[0]) if cov else start, now, False, now)
            fcov = self.store.coverage_get("funding", coin)
            f_head = fcov is not None and fcov[0] <= start + HOUR
            if not f_head or now - fcov[1] > HOUR:
                f_from = max(start, fcov[1] - HOUR) if f_head else start
                self.store.funding_put(coin, await self.client.funding_history(coin, f_from, now))
                self.store.coverage_set("funding", coin, min(start, fcov[0]) if fcov else start, now, False, now)
            if n % 10 == 0:
                log.info("market_progress", coins=n, of=len(starts), weight_spent=round(self.client.limiter.spent))
        log.info("market_fetched", coins=len(starts), weight_spent=round(self.client.limiter.spent))


def _chunks(items: list[str], n: int) -> list[list[str]]:
    return [items[i : i + n] for i in range(0, len(items), n)]


# --- loading from the cache (no network) -----------------------------------------------------------------------


def deep_addresses(store: Store) -> list[str]:
    """Wallets whose fills are in the cache (stage 2 done at least once)."""
    return sorted(r[0] for r in store.db.execute("SELECT key FROM coverage WHERE kind = 'fills'"))


def load_wallet(store: Store, addr: str) -> WalletData:
    cov = store.coverage_get("fills", addr)
    lb = store.leaderboard_row(addr)
    return WalletData(
        address=addr,
        raw_fills=store.fills_get(addr),
        fills_truncated=bool(cov and cov[2]),
        history_from=cov[0] if cov and not cov[2] else None,
        portfolio=store.kv_get("portfolio", addr),
        clearinghouse=store.kv_get("clearinghouse", addr),
        spot_state=store.kv_get("spot", addr),
        ledger=store.ledger_get(addr),
        role=store.kv_get("role", addr),
        leaderboard=lb,
        sources=store.addresses().get(addr, set()),
    )


def load_market(store: Store, coins: set[str], cfg: Config) -> MarketData:
    meta = parse_meta(store.kv_get("meta", "perp"))
    spot = parse_spot_prices(store.kv_get("meta", "spot"))
    bars: dict[str, Bars] = {}
    funding: dict[str, Funding] = {}
    for coin in coins:
        series = {i: store.candles_get(coin, i) for i in cfg.api.candle_intervals}
        bars[coin] = Bars.merge({i: rows for i, rows in series.items() if rows})
        rows = store.funding_get(coin)
        funding[coin] = Funding(
            np.array([r[0] for r in rows], dtype=np.int64), np.array([r[1] for r in rows], dtype=float)
        )
    rg = cfg.rules.regime
    hourly = store.candles_get(rg.reference_coin, "1h")
    regime = build_regime(hourly, rg.vol_window_h, rg.extreme_quantile) if hourly else None
    return MarketData(meta=meta, bars=bars, funding=funding, spot_prices=spot, regime=regime)


def market_starts(wallets: list[WalletData], allow_hip3: bool, margin_ms: int = 2 * DAY) -> dict[str, int]:
    """coin → from when market data is needed: the earliest fill of any wallet in it, or the start of that
    wallet's history if the position was already open at its first fill (the MTM curve marks it from there)."""
    out: dict[str, int] = {}
    for w in wallets:
        fills = sorted(w.raw_fills, key=lambda f: int(f["time"]))
        if not fills:
            continue
        head = w.history_from if w.history_from is not None else int(fills[0]["time"])
        seen: set[str] = set()
        for f in fills:
            coin = str(f.get("coin", ""))
            if coin in seen or not is_perp_coin(coin, allow_hip3):
                continue
            seen.add(coin)
            t = int(f["time"])
            if abs(float(f.get("startPosition") or 0.0)) > 0:
                t = min(t, head)
            out[coin] = min(out.get(coin, t), t - margin_ms)
    return out


def traded_coins(wallets: list[WalletData], allow_hip3: bool) -> set[str]:
    return {
        str(f.get("coin")) for w in wallets for f in w.raw_fills if is_perp_coin(str(f.get("coin", "")), allow_hip3)
    }
