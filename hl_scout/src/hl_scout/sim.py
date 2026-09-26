"""Simulation of MY copy of a wallet through the third-party copy bot.

The bot's behaviour is taken from `CopySemantics` (copybot_fields.yaml); nothing about it is hard-coded here.
Exchange facts (min order, fees, maintenance margin, hourly funding) come from docs/api_notes.md §7.

Model, in order of events:
- a trader action (aggregated fills of one order) is seen by the bot and executed `delay` later;
- execution price = trader's VWAP moved by the market between t and t+delay (interpolated bars) + slippage,
  + a volatility penalty when the bars are coarser than 1 minute (the move inside the delay is unresolved);
- between my executions positions are constant; bars are scanned (vectorised) for liquidation, Price SL/TP
  and Balance SL, funding is charged hourly on my position.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field, replace

import numpy as np

from hl_scout.analytics import Action
from hl_scout.config import Config, CopySemantics
from hl_scout.market import MarketData
from hl_scout.util import MIN, round_size_down

EPS = 1e-9


@dataclass(frozen=True)
class CopySettings:
    """One point of the copy-bot settings space (field names follow copybot_fields.yaml)."""

    alloc_usd: float
    copy_ratio: float
    leverage: int
    min_trade_usd: float = 10.0
    max_trade_usd: float | None = None
    buy_times: int = 0  # Buy Times Per Token; 0 = unlimited
    small_size: str = "skip"  # Your Copy Size < $10: "skip" | "buy"
    max_tokens: int = 0  # Max number of tokens; 0 = unlimited
    max_token_size_usd: float | None = None
    max_token_margin_usd: float | None = None
    max_total_margin_usd: float | None = None
    price_sl_pct: float | None = None  # fraction of price (or ROE, per semantics)
    price_tp_pct: float | None = None
    balance_sl_usd: float | None = None
    balance_tp_usd: float | None = None
    copy_long: bool = True
    copy_short: bool = True
    reverse: bool = False
    target_usd: float = 0.0  # the position size the ratio was derived from (informational)
    label: str = ""


@dataclass(frozen=True)
class SimEnv:
    delay_ms: int
    taker_fee: float  # fraction of notional
    bot_fee: float
    slippage: dict[str, float]  # fraction per coin
    delay_penalty_k: float
    stop_slippage: float
    semantics: CopySemantics
    ideal: bool = False  # reference copy: no delay, costs, minimum size or caps

    @classmethod
    def from_config(
        cls, cfg: Config, market: MarketData, semantics: CopySemantics, bot_fee_bps: float, delay_s: float | None = None
    ) -> SimEnv:
        return cls(
            delay_ms=int(1000 * (cfg.copying.delay_s if delay_s is None else delay_s)),
            taker_fee=cfg.costs.taker_fee_bps / 1e4,
            bot_fee=bot_fee_bps / 1e4,
            slippage={c: cfg.costs.slippage_bps(m.day_volume) / 1e4 for c, m in market.meta.items()},
            delay_penalty_k=cfg.costs.delay_penalty_k,
            stop_slippage=cfg.costs.stop_slippage_pct / 100,
            semantics=semantics,
        )

    def as_ideal(self) -> SimEnv:
        return replace(
            self,
            delay_ms=0,
            taker_fee=0.0,
            bot_fee=0.0,
            slippage={},
            delay_penalty_k=0.0,
            stop_slippage=0.0,
            ideal=True,
        )


@dataclass
class _Pos:
    size: float  # signed coins
    entry: float
    margin: float
    n_buys: int
    t_open: int
    sl_px: float | None = None
    tp_px: float | None = None
    peak_notional: float = 0.0


@dataclass
class StopEvent:
    t: int
    kind: str  # liquidation | price_sl | price_tp | balance_sl | balance_tp
    coin: str | None
    pnl: float


@dataclass
class SimResult:
    start: float
    end: float
    t_start: int
    t_end: int
    path_t: np.ndarray
    path_eq: np.ndarray
    path_worst: np.ndarray
    path_notional: np.ndarray
    fees: float
    slippage_cost: float
    funding: float  # net received (+) / paid (−)
    outcomes: Counter
    stops: list[StopEvent]
    closed_pnls: list[float]  # my realized trips (net of fees)
    max_margin: float
    max_positions: int
    stopped_at: int | None = None
    liquidated: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def pnl(self) -> float:
        return self.end - self.start

    @property
    def costs(self) -> float:
        return self.fees + self.slippage_cost + max(0.0, -self.funding)

    @property
    def gross_pnl(self) -> float:
        """PnL before fees, slippage and funding paid."""
        return self.pnl + self.costs

    def equity_at(self, t: float) -> float:
        if len(self.path_t) == 0:
            return self.start
        return float(np.interp(t, self.path_t, self.path_eq, left=self.start, right=self.end))

    def equity_series(self, times: np.ndarray) -> np.ndarray:
        if len(self.path_t) == 0:
            return np.full(len(times), self.start)
        return np.interp(times, self.path_t, self.path_eq, left=self.start, right=self.end)

    _KINDS = ("open", "increase", "late_entry", "reduce")

    def lost_action_share(self) -> float:
        """Entries, adds and partial closes whose copy would be below the minimum (skipped or rounded up)."""
        o = self.outcomes
        lost_kinds = ("skipped_small", "bumped", "no_target_balance")
        considered = sum(o[f"{k}_{s}"] for k in self._KINDS for s in ("copied", *lost_kinds))
        lost = sum(o[f"{k}_{s}"] for k in self._KINDS for s in lost_kinds)
        return lost / considered if considered else 0.0

    def daily_returns(self, t0: int, n_days: int) -> np.ndarray:
        edges = t0 + np.arange(n_days + 1, dtype=np.int64) * 86_400_000
        eq = self.equity_series(edges)
        prev = eq[:-1]
        return np.where(prev > 0, np.diff(eq) / np.maximum(prev, 1e-9), 0.0)


def _kind(a: Action) -> str:
    return a.kind


class CopySimulator:
    def __init__(self, market: MarketData, env: SimEnv) -> None:
        self.market = market
        self.env = env

    # ------------------------------------------------------------------------------------------------
    def run(
        self,
        actions: list[Action],
        s: CopySettings,
        t_start: int,
        t_end: int,
        pause: list[tuple[int, int]] | None = None,
    ) -> SimResult:
        """Copy the trader's actions seen in [t_start, t_end). Positions left open are marked at t_end."""
        run = _Run(self, s, t_start, t_end, pause or [])
        delay = 0 if self.env.ideal else self.env.delay_ms
        for a in actions:
            if a.t < t_start:
                continue
            te = a.t + delay
            if te >= t_end:
                break
            run.advance(te)
            if run.stopped:
                break
            run.handle(a, te)
        return run.finish()


class _Run:
    def __init__(self, sim: CopySimulator, s: CopySettings, t_start: int, t_end: int, pause: list[tuple[int, int]]):
        self.sim, self.s, self.env, self.m = sim, s, sim.env, sim.market
        self.sem = sim.env.semantics
        self.t = t_start
        self.t_start, self.t_end = t_start, t_end
        self.cash = s.alloc_usd
        self.pos: dict[str, _Pos] = {}
        self.pause = sorted(pause)
        self.outcomes: Counter = Counter()
        self.stops: list[StopEvent] = []
        self.closed: list[float] = []
        self.fees = self.slip = self.funding = 0.0
        self.max_margin = 0.0
        self.max_positions = 0
        self.stopped = False
        self.stopped_at: int | None = None
        self.liquidated = False
        self._scan_start_equity = self.cash
        self._trip_pnl: dict[str, float] = {}
        self.pt: list[np.ndarray] = [np.array([t_start], dtype=np.int64)]
        self.pe: list[np.ndarray] = [np.array([self.cash])]
        self.pw: list[np.ndarray] = [np.array([self.cash])]
        self.pn: list[np.ndarray] = [np.array([0.0])]

    # --- helpers ------------------------------------------------------------------------------------------
    def _px(self, coin: str, t: float) -> float:
        return self.m.price_at(coin, t)

    def _equity(self, t: float) -> float:
        return self.cash + sum(p.size * (self._px(c, t) - p.entry) for c, p in self.pos.items())

    def _margin_used(self) -> float:
        return sum(p.margin for p in self.pos.values())

    def _record(self, t: float) -> None:
        eq, notional = self.cash, 0.0
        for c, p in self.pos.items():
            px = self._px(c, t)
            eq += p.size * (px - p.entry)
            notional += abs(p.size) * px
        self.pt.append(np.array([int(t)], dtype=np.int64))
        self.pe.append(np.array([eq]))
        self.pw.append(np.array([eq]))
        self.pn.append(np.array([notional]))

    def _paused(self, t: float) -> bool:
        return any(a <= t < b for a, b in self.pause)

    def _exec_px(self, a: Action, te: int, side: int) -> float:
        """Price I get: trader VWAP moved by the market over the delay, plus costs against me."""
        if self.env.ideal:
            return a.px
        bars = self.m.bars.get(a.coin)
        base = a.px
        penalty = 0.0
        if bars is not None and len(bars):
            p0, p1 = bars.price_at(a.t), bars.price_at(te)
            if p0 > 0 and p1 > 0 and math.isfinite(p0) and math.isfinite(p1):
                base = a.px * p1 / p0
            if bars.resolution_at(te) > MIN and self.env.delay_ms > 0:
                penalty = self.env.delay_penalty_k * bars.sigma_per_sqrt_ms() * math.sqrt(self.env.delay_ms)
        slip = self.env.slippage.get(a.coin, max(self.env.slippage.values(), default=0.002))
        return base * (1.0 + side * (slip + penalty))

    def _fee(self, notional: float) -> float:
        return notional * (self.env.taker_fee + self.env.bot_fee)

    def _sz_decimals(self, coin: str) -> int:
        meta = self.m.meta.get(coin)
        return meta.sz_decimals if meta else 4

    def _mm_rate(self, coin: str) -> float:
        meta = self.m.meta.get(coin)
        return meta.mm_rate if meta else 1.0 / 40

    # --- sizing rules (Min/Max Trade Size, token caps, margin caps) -----------------------------------------
    def _size_rules(self, coin: str, notional: float, px: float, kind: str) -> tuple[float, str]:
        s = self.s
        if self.env.ideal:
            return notional, "copied"
        outcome = "copied"
        if notional < s.min_trade_usd - EPS:
            if s.small_size == "skip":
                return 0.0, "skipped_small"
            notional, outcome = s.min_trade_usd, "bumped"
        if s.max_trade_usd is not None and notional > s.max_trade_usd:
            notional = s.max_trade_usd
        cur = self.pos.get(coin)
        cur_notional = abs(cur.size) * px if cur else 0.0
        cur_margin = cur.margin if cur else 0.0
        if s.max_token_size_usd is not None:
            notional = min(notional, s.max_token_size_usd - cur_notional)
        lev = max(1, s.leverage)
        margin = notional / lev
        caps = [self._equity(self.t) - self._margin_used()]
        if s.max_token_margin_usd is not None:
            caps.append(s.max_token_margin_usd - cur_margin)
        if s.max_total_margin_usd is not None:
            caps.append(s.max_total_margin_usd - self._margin_used())
        margin = min(margin, *caps)
        if margin <= 0 or notional <= 0:
            return 0.0, "skipped_limits"
        notional = margin * lev
        size = round_size_down(notional / px, self._sz_decimals(coin))
        notional = size * px
        if notional < s.min_trade_usd - EPS or size <= 0:
            return 0.0, "skipped_limits"
        return notional, outcome

    # --- order handlers -----------------------------------------------------------------------------------
    def handle(self, a: Action, te: int) -> None:
        kind = _kind(a)
        sign = -1 if self.s.reverse else 1
        self.outcomes["trader_" + kind] += 1
        if kind == "open":
            self._open(a, te, sign * (1 if a.pos_after > 0 else -1), self._factor(a, te) * a.sz, "open")
        elif kind == "increase":
            if a.coin in self.pos:
                self._increase(a, te)
            elif self.sem.increase_without_position == "open":
                # my entry was skipped earlier but the bot mirrors this add as a fresh (late) entry
                self._open(a, te, sign * (1 if a.pos_after > 0 else -1), self._factor(a, te) * a.sz, "late_entry")
            else:
                self.outcomes["increase_no_position"] += 1
        elif kind == "reduce":
            self._reduce(a, te)
        elif kind == "close":
            self._close_on_flat(a, te)
        elif kind == "flip":
            self._close_on_flat(a, te)
            self._open(a, te, sign * (1 if a.pos_after > 0 else -1), self._factor(a, te) * abs(a.pos_after), "open")
        self.max_positions = max(self.max_positions, len(self.pos))
        self.max_margin = max(self.max_margin, self._margin_used())
        self._check_balance_after_event(te)
        self._record(te)

    def _factor(self, a: Action, te: int) -> float:
        """Copy size per unit of the trader's size, as the bot computes it (copybot_fields.yaml → semantics)."""
        if self.sem.ratio_applies_to == "balance_scaled":
            # Your Copy Size = (Target Size ÷ Target Balance) × Your Balance × Copy Ratio
            if a.trader_equity <= 0:
                return 0.0
            return self.s.copy_ratio * max(self._equity(te), 0.0) / a.trader_equity
        return self.s.copy_ratio

    def _open(self, a: Action, te: int, direction: int, size_coins: float, kind: str) -> None:
        s = self.s
        if size_coins <= 0:
            self.outcomes[f"{kind}_no_target_balance"] += 1
            return
        if (direction > 0 and not s.copy_long) or (direction < 0 and not s.copy_short):
            self.outcomes[f"{kind}_side_off"] += 1
            return
        if self._paused(te):
            self.outcomes[f"{kind}_paused"] += 1
            return
        existing = self.pos.get(a.coin)
        if existing is not None and (existing.size > 0) != (direction > 0):
            self._close_position(a.coin, te, self._exec_px(a, te, -1 if existing.size > 0 else 1), "flip_cleanup")
            existing = None
        if existing is None and s.max_tokens and len(self.pos) >= s.max_tokens:
            self.outcomes[f"{kind}_skipped_max_tokens"] += 1
            return
        if existing is not None:  # already holding this coin in the same direction: treat as an add
            self._add(a.coin, a, te, direction, size_coins, kind)
            return
        px = self._exec_px(a, te, direction)
        notional, outcome = self._size_rules(a.coin, size_coins * px, px, kind)
        self.outcomes[f"{kind}_{outcome}"] += 1
        if notional <= 0:
            return
        size = direction * notional / px
        fee = self._fee(notional)
        self.cash -= fee
        self.fees += fee
        self.slip += abs(px - a.px) * abs(size) if not self.env.ideal else 0.0
        p = _Pos(size=size, entry=px, margin=notional / max(1, s.leverage), n_buys=1, t_open=te, peak_notional=notional)
        self._set_stops(p)
        self.pos[a.coin] = p
        self._trip_pnl[a.coin] = -fee

    def _set_stops(self, p: _Pos) -> None:
        s, d = self.s, (1 if p.size > 0 else -1)
        scale = 1.0 / max(1, s.leverage) if self.sem.price_sl_basis == "roe" else 1.0
        if s.price_sl_pct is not None and not self.env.ideal:
            p.sl_px = p.entry * (1 - d * s.price_sl_pct * scale)
        if s.price_tp_pct is not None and not self.env.ideal:
            p.tp_px = p.entry * (1 + d * s.price_tp_pct * scale)

    def _increase(self, a: Action, te: int) -> None:
        p = self.pos[a.coin]
        direction = 1 if p.size > 0 else -1
        self._add(a.coin, a, te, direction, self._factor(a, te) * a.sz, "increase")

    def _add(self, coin: str, a: Action, te: int, direction: int, size_coins: float, kind: str) -> None:
        s, p = self.s, self.pos[coin]
        if size_coins <= 0:
            self.outcomes[f"{kind}_no_target_balance"] += 1
            return
        limit = s.buy_times
        if limit and p.n_buys >= limit:
            self.outcomes[f"{kind}_skipped_buy_times"] += 1
            return
        if self._paused(te):
            self.outcomes[f"{kind}_paused"] += 1
            return
        px = self._exec_px(a, te, direction)
        notional, outcome = self._size_rules(coin, size_coins * px, px, kind)
        self.outcomes[f"{kind}_{outcome}"] += 1
        if notional <= 0:
            return
        add = direction * notional / px
        fee = self._fee(notional)
        self.cash -= fee
        self.fees += fee
        self.slip += abs(px - a.px) * abs(add) if not self.env.ideal else 0.0
        new_size = p.size + add
        p.entry = (p.entry * abs(p.size) + px * abs(add)) / abs(new_size)
        p.size = new_size
        p.margin += notional / max(1, s.leverage)
        p.n_buys += 1
        p.peak_notional = max(p.peak_notional, abs(new_size) * px)
        self._set_stops(p)
        self._trip_pnl[coin] = self._trip_pnl.get(coin, 0.0) - fee

    def _reduce(self, a: Action, te: int) -> None:
        p = self.pos.get(a.coin)
        if p is None:
            self.outcomes["reduce_no_position"] += 1
            return
        side = -1 if p.size > 0 else 1
        px = self._exec_px(a, te, side)
        if self.sem.reduce_mode == "ratio_of_order" and not self.env.ideal:
            qty = min(self._factor(a, te) * a.sz, abs(p.size))
        else:
            frac = min(1.0, a.sz / abs(a.pos_before)) if a.pos_before else 1.0
            qty = abs(p.size) * frac
        notional = qty * px
        outcome = "copied"
        if not self.env.ideal and self.sem.small_size_applies_to_reduce and notional < self.s.min_trade_usd - EPS:
            if self.s.small_size == "skip":
                self.outcomes["reduce_skipped_small"] += 1
                return
            qty = min(abs(p.size), self.s.min_trade_usd / px)
            outcome = "bumped"
        if not self.env.ideal:
            qty = round_size_down(qty, self._sz_decimals(a.coin)) if qty < abs(p.size) else qty
        if qty <= 0:
            self.outcomes["reduce_skipped_small"] += 1
            return
        self.outcomes[f"reduce_{outcome}"] += 1
        self._realize(a.coin, qty, px, a.px)

    def _close_on_flat(self, a: Action, te: int) -> None:
        p = self.pos.get(a.coin)
        if p is None:
            self.outcomes["close_no_position"] += 1
            return
        if not self.sem.full_close_on_trader_flat:
            self._reduce(a, te)
            return
        side = -1 if p.size > 0 else 1
        px = self._exec_px(a, te, side)
        if not self.env.ideal and abs(p.size) * px < self.s.min_trade_usd - EPS and not self.sem.allow_small_full_close:
            self.outcomes["close_skipped_small"] += 1
            return
        self.outcomes["close_copied"] += 1
        self._close_position(a.coin, te, px, "trader_close", ref_px=a.px)

    def _realize(self, coin: str, qty: float, px: float, ref_px: float | None = None) -> None:
        p = self.pos[coin]
        d = 1 if p.size > 0 else -1
        qty = min(qty, abs(p.size))
        pnl = d * qty * (px - p.entry)
        notional = qty * px
        fee = self._fee(notional)
        frac = qty / abs(p.size)
        self.cash += pnl - fee
        self.fees += fee
        if ref_px is not None and not self.env.ideal:
            self.slip += abs(px - ref_px) * qty
        self._trip_pnl[coin] = self._trip_pnl.get(coin, 0.0) + pnl - fee
        p.margin *= 1 - frac
        p.size -= d * qty
        if abs(p.size) * px < 1e-6:
            self.closed.append(self._trip_pnl.pop(coin, 0.0))
            del self.pos[coin]

    def _close_position(self, coin: str, t: float, px: float, reason: str, ref_px: float | None = None) -> None:
        if coin in self.pos:
            self._realize(coin, abs(self.pos[coin].size), px, ref_px)
            self.outcomes["exit_" + reason] += 1

    # --- time evolution -----------------------------------------------------------------------------------
    def advance(self, t_target: int) -> None:
        guard = 0
        while self.t < t_target and not self.stopped and guard < 10_000:
            guard += 1
            if not self.pos:
                self.t = t_target
                return
            if not self._scan(t_target):
                return

    def _scan(self, t_target: int) -> bool:
        """Scan bars in (self.t, t_target]. Returns True if an intra-interval event happened (continue scanning)."""
        coins = list(self.pos)
        self._scan_start_equity = self._equity(self.t)
        grids = []
        for c in coins:
            b = self.m.bars.get(c)
            if b is None or not len(b):
                continue
            sl = b.overlapping(self.t, t_target)
            grids.append(np.minimum(b.t1[sl], t_target))
        times = np.unique(np.concatenate([*grids, np.array([t_target], dtype=np.int64)]))
        times = times[times > self.t]
        if not len(times):
            self.t = t_target
            return False
        k = len(times)
        eq_close = np.full(k, self.cash)
        eq_worst = np.full(k, self.cash)
        notional = np.zeros(k)
        mm = np.zeros(k)
        fund_cum = np.zeros(k)
        per_coin: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        for c in coins:
            p, b = self.pos[c], self.m.bars.get(c)
            if b is None or not len(b):
                close = worst = best = np.full(k, p.entry)
            else:
                idx = np.clip(np.searchsorted(b.t1, times, side="left"), 0, len(b) - 1)
                close = b.prices_at(times)
                lo, hi = b.l[idx].copy(), b.h[idx].copy()
                # the bar in which the interval starts: only its part after self.t counts
                first = (b.t0[idx] < self.t) & (self.t < b.t1[idx])
                if first.any():
                    p_start = b.price_at(self.t)
                    lo[first] = np.minimum(p_start, close[first])
                    hi[first] = np.maximum(p_start, close[first])
                worst = lo if p.size > 0 else hi
                best = hi if p.size > 0 else lo
            eq_close += p.size * (close - p.entry)
            eq_worst += p.size * (worst - p.entry)
            notional += np.abs(p.size) * close
            mm += np.abs(p.size) * worst * self._mm_rate(c)
            per_coin[c] = (close, worst, best)
            fund = self.m.funding.get(c)
            if fund is not None and b is not None and len(b) and not self.env.ideal:
                ft, fr = fund.between(self.t, t_target)
                if len(ft):
                    pay = -p.size * b.prices_at(ft) * fr  # + received, − paid (hourly [api_notes §7])
                    cum = np.cumsum(pay)
                    j = np.searchsorted(ft, times, side="right") - 1
                    fund_cum += np.where(j >= 0, cum[np.clip(j, 0, None)], 0.0)
        eq_close += fund_cum
        eq_worst += fund_cum
        if self.env.ideal:
            return self._commit(times, eq_close, eq_worst, notional, fund_cum, k - 1, t_target)
        # --- triggers (first bar where any fires) ---
        n_trig = k
        trig: list[tuple[str, str | None]] = []
        for c in coins:
            p = self.pos[c]
            close, worst, best = per_coin[c]
            if p.sl_px is not None:
                hit = (worst <= p.sl_px) if p.size > 0 else (worst >= p.sl_px)
                n_trig, trig = _earliest(hit, n_trig, trig, ("price_sl", c))
            if p.tp_px is not None:
                hit = (best >= p.tp_px) if p.size > 0 else (best <= p.tp_px)
                n_trig, trig = _earliest(hit, n_trig, trig, ("price_tp", c))
            if self.sem.margin_mode == "isolated":
                pnl_worst = p.size * (worst - p.entry)
                hit = p.margin + pnl_worst <= np.abs(p.size) * worst * self._mm_rate(c)
                n_trig, trig = _earliest(hit, n_trig, trig, ("liquidation", c))
        if self.sem.margin_mode == "cross":
            n_trig, trig = _earliest(eq_worst <= mm, n_trig, trig, ("liquidation", None))
        bsl = self._balance_sl_level()
        if bsl is not None:
            n_trig, trig = _earliest(eq_worst <= bsl, n_trig, trig, ("balance_sl", None))
        if n_trig >= k:
            return self._commit(times, eq_close, eq_worst, notional, fund_cum, k - 1, t_target)
        # commit the path up to the trigger bar, then apply the event (stops before liquidation: on a continuous
        # path a stop placed closer than the liquidation price fires first)
        t_hit = int(times[n_trig])
        self._commit(times, eq_close, eq_worst, notional, fund_cum, n_trig - 1, None)
        self.cash += float(fund_cum[n_trig] - (fund_cum[n_trig - 1] if n_trig > 0 else 0.0))
        self.funding += float(fund_cum[n_trig] - (fund_cum[n_trig - 1] if n_trig > 0 else 0.0))
        self.t = t_hit
        order = {"price_sl": 0, "balance_sl": 1, "liquidation": 2, "price_tp": 3}
        kind, coin = sorted(trig, key=lambda x: order[x[0]])[0]
        self._apply_trigger(kind, coin, t_hit, per_coin, n_trig)
        self._record(t_hit)
        return True

    def _commit(
        self,
        times: np.ndarray,
        eq_close: np.ndarray,
        eq_worst: np.ndarray,
        notional: np.ndarray,
        fund_cum: np.ndarray,
        last: int,
        t_final: int | None,
    ) -> bool:
        if last >= 0:
            self.pt.append(times[: last + 1].astype(np.int64))
            self.pe.append(eq_close[: last + 1])
            self.pw.append(eq_worst[: last + 1])
            self.pn.append(notional[: last + 1])
        if t_final is not None:
            self.cash += float(fund_cum[-1])
            self.funding += float(fund_cum[-1])
            self.t = t_final
            return False
        if last >= 0:
            self.cash += float(fund_cum[last])
            self.funding += float(fund_cum[last])
        return False

    def _balance_sl_level(self) -> float | None:
        s = self.s
        if s.balance_sl_usd is None:
            return None
        return s.balance_sl_usd if self.sem.balance_sl_basis == "level" else s.alloc_usd - s.balance_sl_usd

    def _apply_trigger(self, kind: str, coin: str | None, t: int, per_coin: dict, i: int) -> None:
        slip = self.env.stop_slippage
        before = self._equity(t)
        if kind == "price_sl" and coin is not None:
            p = self.pos[coin]
            d = 1 if p.size > 0 else -1
            assert p.sl_px is not None
            px = p.sl_px * (1 - d * slip)
            self._close_position(coin, t, px, "price_sl")
        elif kind == "price_tp" and coin is not None:
            p = self.pos[coin]
            assert p.tp_px is not None
            self._close_position(coin, t, p.tp_px, "price_tp")
        elif kind == "balance_sl":
            # on a continuous path the bot exits at the level; if equity was already below it when the bar
            # started (gap), it exits at that lower equity
            level = self._balance_sl_level() or 0.0
            self._balance_stop(t, exit_equity=min(level, self._scan_start_equity))
        elif kind == "liquidation":
            if coin is not None:  # isolated: lose this position's margin
                p = self.pos.pop(coin)
                self.cash -= p.margin
                self.closed.append(self._trip_pnl.pop(coin, 0.0) - p.margin)
            else:  # cross: backstop liquidation keeps maintenance margin → nothing left [api_notes §7]
                for c in list(self.pos):
                    self.closed.append(self._trip_pnl.pop(c, 0.0))
                self.pos.clear()
                self.cash = 0.0
                self._stop(t)
            self.liquidated = True
            self.outcomes["exit_liquidation"] += 1
        self.stops.append(StopEvent(t, kind, coin, self._equity(t) - before))

    def _balance_stop(self, t: int, exit_equity: float) -> None:
        """Balance SL: the bot sells everything once equity touches the level; the exit is a bit worse."""
        notional = sum(abs(p.size) * self._px(c, t) for c, p in self.pos.items())
        fee = self._fee(notional)
        self.fees += fee
        for c in list(self.pos):
            self.closed.append(self._trip_pnl.pop(c, 0.0))
        self.pos.clear()
        self.cash = max(0.0, exit_equity * (1 - self.env.stop_slippage) - fee)
        self.outcomes["exit_balance_sl"] += 1
        self._stop(t)

    def _check_balance_after_event(self, t: int) -> None:
        """A realized loss or fee can drop equity below the level between bar scans: stop at current equity."""
        level = self._balance_sl_level()
        if level is not None and not self.env.ideal and not self.stopped:
            eq = self._equity(t)
            if eq <= level:
                self._balance_stop(t, exit_equity=eq)
                self.stops.append(StopEvent(t, "balance_sl", None, self._equity(t) - eq))

    def _stop(self, t: int) -> None:
        self.stopped = True
        self.stopped_at = t

    # --- end ------------------------------------------------------------------------------------------------
    def finish(self) -> SimResult:
        if not self.stopped:
            self.advance(self.t_end)
        t = self.t_end if not self.stopped else (self.stopped_at or self.t_end)
        for c in list(self.pos):
            p = self.pos[c]
            px = self._px(c, t)
            px = px * (1 - (1 if p.size > 0 else -1) * self.env.slippage.get(c, 0.0))
            self._close_position(c, t, px, "window_end")
        end = self.cash
        self.pt.append(np.array([self.t_end], dtype=np.int64))
        self.pe.append(np.array([end]))
        self.pw.append(np.array([end]))
        self.pn.append(np.array([0.0]))
        pt = np.concatenate(self.pt)
        order = np.argsort(pt, kind="stable")
        return SimResult(
            start=self.s.alloc_usd,
            end=end,
            t_start=self.t_start,
            t_end=self.t_end,
            path_t=pt[order],
            path_eq=np.concatenate(self.pe)[order],
            path_worst=np.concatenate(self.pw)[order],
            path_notional=np.concatenate(self.pn)[order],
            fees=self.fees,
            slippage_cost=self.slip,
            funding=self.funding,
            outcomes=self.outcomes,
            stops=self.stops,
            closed_pnls=self.closed,
            max_margin=self.max_margin,
            max_positions=self.max_positions,
            stopped_at=self.stopped_at,
            liquidated=self.liquidated,
        )


def _earliest(hit: np.ndarray, n_best: int, trig: list, tag: tuple[str, str | None]) -> tuple[int, list]:
    if not hit.any():
        return n_best, trig
    i = int(np.argmax(hit))
    if i < n_best:
        return i, [tag]
    if i == n_best:
        return n_best, [*trig, tag]
    return n_best, trig
