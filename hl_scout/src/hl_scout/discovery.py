"""Candidate discovery and data collection into the SQLite cache.

Pool = leaderboard rows selected by ACTIVITY (volume), not by PnL, + addresses seen in large trades + my own
addresses. Stage 1 fetches only `portfolio` (cheap) and keeps wallets that pass a relaxed screen now or at any
walk-forward fold start; stage 2 fetches everything else (fills, positions, ledger, spot, role) for survivors.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from hl_scout.analytics import build_equity_curve
from hl_scout.backtest import make_folds
from hl_scout.config import Config
from hl_scout.hl.client import INTERVAL_MS, InfoClient, gather_limited
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


def select_pool(rows: list[dict[str, Any]], cfg: Config, extra: dict[str, float], manual: list[str]) -> list[str]:
    """Activity-based pool (no PnL filter → no survivorship selection on results) + large-trade + manual."""
    d = cfg.discovery

    def vol(r: dict[str, Any], w: str) -> float:
        return float((r["perf"].get(w) or {}).get("vlm") or 0.0)

    active = [
        r
        for r in rows
        if vol(r, "month") >= d.pool_min_month_volume_usd or vol(r, "allTime") >= d.pool_min_alltime_volume_usd
    ]
    active.sort(key=lambda r: (vol(r, "month"), vol(r, "allTime")), reverse=True)
    pool: list[str] = []
    seen: set[str] = set()
    for a in [*manual, *sorted(extra, key=lambda a: -extra[a])]:
        if a not in seen:
            pool.append(a)
            seen.add(a)
    for r in active:
        if len(pool) >= d.pool_max:
            break
        if r["address"] not in seen:
            pool.append(r["address"])
            seen.add(r["address"])
    return pool


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
        coins = traded_coins([load_wallet(store, a) for a in done], cfg.universe.allow_hip3)
        coins.add(cfg.rules.regime.reference_coin)
        await self.market_fetch(sorted(coins))
        log.info("discovery_done", pool=len(pool), deep=len(done), requests=self.client.requests,
                 weight=round(self.client.limiter.spent))
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
        if not coins:
            return 0
        trades = await collect_large_trades(
            self.cfg.api.ws_url, coins, lt.min_notional_usd, 60 * (minutes or lt.listen_min)
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
        return select_pool(rows, self.cfg, extra, manual)

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
        results = await gather_limited([self.portfolio(a) for a in pool], self.cfg.api.max_concurrency)
        keep: list[str] = []
        for addr, res in zip(pool, results, strict=True):
            if isinstance(res, Exception):
                log.warning("portfolio_failed", address=addr, err=str(res))
                continue
            curve = build_equity_curve(res, prefer="perp")
            total = build_equity_curve(res, prefer="total")
            manual = addr in self._manual()
            if manual or any(stage1_pass(curve, total, t, self.cfg) for t in checkpoints):
                keep.append(addr)
        log.info("stage1_done", pool=len(pool), kept=len(keep))
        return keep[: self.cfg.discovery.deep_max]

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
    async def market_fetch(self, coins: list[str]) -> None:
        now = now_ms()
        start = now - self.cfg.discovery.history_days * DAY
        for coin in coins:
            for interval in self.cfg.api.candle_intervals:
                key = f"{coin}:{interval}"
                cov = self.store.coverage_get("candles", key)
                if cov is not None and now - cov[1] < INTERVAL_MS[interval]:
                    continue
                c_from = start if cov is None else max(start, cov[1] - 2 * INTERVAL_MS[interval])
                candles = await self.client.candles(coin, interval, c_from, now)
                self.store.candles_put(coin, interval, candles)
                self.store.coverage_set("candles", key, min(start, cov[0]) if cov else start, now, False, now)
            fcov = self.store.coverage_get("funding", coin)
            if fcov is None or now - fcov[1] > HOUR:
                f_from = start if fcov is None else max(start, fcov[1] - HOUR)
                self.store.funding_put(coin, await self.client.funding_history(coin, f_from, now))
                self.store.coverage_set("funding", coin, min(start, fcov[0]) if fcov else start, now, False, now)
        log.info("market_fetched", coins=len(coins), weight_spent=round(self.client.limiter.spent))


def _chunks(items: list[str], n: int) -> list[list[str]]:
    return [items[i : i + n] for i in range(0, len(items), n)]


# --- loading from the cache (no network) -----------------------------------------------------------------------


def deep_addresses(store: Store) -> list[str]:
    """Wallets whose fills are in the cache (stage 2 done at least once)."""
    return sorted(r[0] for r in store.db.execute("SELECT key FROM coverage WHERE kind = 'fills'"))


def load_wallet(store: Store, addr: str) -> WalletData:
    cov = store.coverage_get("fills", addr)
    lb = next((r for r in store.leaderboard_rows() if r["address"] == addr), None)
    return WalletData(
        address=addr,
        raw_fills=store.fills_get(addr),
        fills_truncated=bool(cov and cov[2]),
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


def traded_coins(wallets: list[WalletData], allow_hip3: bool) -> set[str]:
    return {
        str(f.get("coin")) for w in wallets for f in w.raw_fills if is_perp_coin(str(f.get("coin", "")), allow_hip3)
    }
