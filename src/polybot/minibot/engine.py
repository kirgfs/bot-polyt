"""Mini-bot engine: stage-0 quotes on selected soccer markets, executed on paper.

docs/architecture.md §13. Per market: WAITING (no usable book) → QUOTING ⇄ PAUSED (the
book jumped) → PULLED at kickoff − `pull_before_start_min`, for good. A daily loss limit
halts all quoting until the next UTC day. A position left at kickoff is held to settlement
and paid out from Gamma's final `outcomePrices` (docs/api_notes.md §11).

Fail-closed (CLAUDE.md, rule 6): no book, a dead connection, a desync, an unknown tick,
stale market metadata or a jump all take the market's quotes off.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from polybot.core.config import MiniBotConfig
from polybot.core.logging import get_logger
from polybot.core.timeutil import NS_PER_S, now_ns, ns_to_date
from polybot.data.records import Kind, Record, RecordWriter, Source
from polybot.execution.paper import PaperEvent, PaperFill, PaperRejected, PaperRules, PaperVenue
from polybot.minibot.model import (
    ClosedDay,
    DayStats,
    Known,
    MarketStatus,
    Phase,
    Settlement,
    Status,
)
from polybot.minibot.report import Reporter
from polybot.minibot.selection import Candidate
from polybot.risk.limits import DailyLossGuard, max_position_shares
from polybot.strategy import rewards as reward_math
from polybot.strategy.stage0 import (
    FairValue,
    Quote,
    QuoteParams,
    compute_quotes,
    needs_requote,
    order_size,
)
from polybot.venues.base import BookLevel, BookSnapshot, InstrumentRef, Side, TradePrint, VenueId
from polybot.venues.polymarket.clob_ws import MarketPool
from polybot.venues.polymarket.orderbook import to_decimal, to_price

log = get_logger(__name__)

NS_PER_MIN = 60 * NS_PER_S
STEP_S = 0.5
REWARD_SAMPLE_NS = NS_PER_MIN  # rewards are scored per minute (docs/api_notes.md §9)
# A soccer match is over within ~2 h of kickoff; then poll Gamma until the market closes.
SETTLE_AFTER_START_NS = 2 * 3600 * NS_PER_S
STALE_METADATA_POLLS = 3  # pull quotes after this many selection intervals without a refresh
STATE_FILE = "minibot_state.json"
STATUS_FILE = "minibot_status.json"

MarketFetcher = Callable[[str], Awaitable[dict[str, Any]]]


@dataclass
class Slot:
    cand: Candidate
    ref: InstrumentRef
    fair: FairValue
    tick: Decimal | None
    min_size: Decimal | None
    phase: Phase = Phase.WAITING
    reason: str = "no_book"
    pause_until_ns: int = 0
    dirty: bool = True
    last_quote_ns: int = 0
    retired: bool = False  # dropped from the selection: no quotes, kept while exposed


class Engine:
    def __init__(
        self,
        cfg: MiniBotConfig,
        *,
        venue: PaperVenue,
        pool: MarketPool,
        sink: RecordWriter,
        reporter: Reporter,
        fetch_market: MarketFetcher,
        state_dir: Path,
        clock: Callable[[], int] = now_ns,
    ) -> None:
        self.cfg = cfg
        self.venue = venue
        self.pool = pool
        self.sink = sink
        self.reporter = reporter
        self.fetch_market = fetch_market
        self.state_path = state_dir / STATE_FILE
        self.status_path = state_dir / STATUS_FILE
        self.clock = clock
        q = cfg.quoting
        self.params = QuoteParams(
            ewma_halflife_s=q.ewma_halflife_s,
            jump_ticks=q.jump_ticks,
            jump_cooldown_s=q.jump_cooldown_s,
            max_book_spread=q.max_book_spread,
            rewards_spread_fraction=q.rewards_spread_fraction,
            default_half_spread_ticks=q.default_half_spread_ticks,
            min_half_spread_ticks=q.min_half_spread_ticks,
            requote_ticks=q.requote_ticks,
            skew_ticks=q.skew_ticks,
        )
        self.slots: dict[str, Slot] = {}
        self.known: dict[str, Known] = {}
        self.marks: dict[str, float] = {}
        self.deposit = float(venue.cash)
        self.day = DayStats(day=self._today(), start_value=float(venue.cash))
        self.loss_guard = DailyLossGuard(cfg.risk.daily_loss_limit_usd)
        self.unreviewed_today: set[str] = set()
        self.meta_fresh_until_ns = 0  # set by each selection; quotes come off after it
        self._last_reward_ns = 0
        self._reports: set[asyncio.Task[None]] = set()
        venue.on_fill = self._on_fill
        venue.on_event = self._on_order_event

    # ------------------------------------------------------------------ selection

    async def apply_selection(self, chosen: list[Candidate]) -> set[str]:
        """Take a fresh selection; returns every token whose book we still need."""
        now = self.clock()
        refresh_ns = int(self.cfg.selection.refresh_s * NS_PER_S)
        self.meta_fresh_until_ns = now + STALE_METADATA_POLLS * refresh_ns
        wanted = {c.token: c for c in chosen}
        for token, slot in list(self.slots.items()):
            if token in wanted:
                continue
            if not slot.retired:
                slot.retired = True
                if slot.phase is not Phase.PULLED:  # keep "before_kickoff" as the reason
                    await self._pull(slot, Phase.PULLED, "deselected")
            if not self._has_exposure(token):
                self._forget(token)
        for token, cand in wanted.items():
            self.known[token] = _known(cand)
            existing = self.slots.get(token)
            if existing is not None:
                existing.cand = cand  # kickoff may have moved, rewards may have changed
                if existing.retired:
                    existing.retired, existing.phase = False, Phase.WAITING
                    existing.reason = "no_book"
                continue
            market = cand.market
            slot = Slot(
                cand=cand,
                ref=InstrumentRef(VenueId.POLYMARKET, market.condition_id, token),
                fair=FairValue(self.params.ewma_halflife_s),
                tick=market.tick_size,
                min_size=market.min_order_size,
            )
            self.slots[token] = slot
            self._set_rules(slot)
            log.info(
                "minibot_market_added",
                title=cand.title,
                label=cand.label,
                start=market.game_start_raw,
                rewards_daily=cand.rewards.daily_rate if cand.rewards else None,
                reviewed=cand.reviewed,
            )
        return set(self.slots)

    def active_tokens(self) -> frozenset[str]:
        """Markets the selection keeps first: quoted now, or holding a position to work off."""
        held = {t for t, h in self.venue.holdings.items() if h.long or h.short}
        return frozenset(t for t, s in self.slots.items() if not s.retired) | held

    def _set_rules(self, slot: Slot) -> None:
        token = slot.cand.token
        if slot.tick is None or slot.min_size is None:
            self.venue.rules.pop(token, None)
            return
        market = slot.cand.market
        self.venue.rules[token] = PaperRules(
            tick=slot.tick,
            min_size=slot.min_size,
            fee_rate=market.fee_rate,
            fee_exponent=market.fee_exponent,
            rebate_rate=market.rebate_rate,
        )

    def _has_exposure(self, token: str) -> bool:
        if any(o.ref.instrument_id == token for o in self.venue.orders.values()):
            return True
        holding = self.venue.holdings.get(token)
        return holding is not None and (holding.long != 0 or holding.short != 0)

    def _forget(self, token: str) -> None:
        self.slots.pop(token, None)
        self.known.pop(token, None)
        self.marks.pop(token, None)
        self.venue.rules.pop(token, None)
        self.venue.books.pop(token, None)
        holding = self.venue.holdings.get(token)
        if holding is not None and holding.long == 0 and holding.short == 0:
            del self.venue.holdings[token]  # realized P&L is already in cash

    # ------------------------------------------------------------------ market data

    def on_ws_event(self, event: dict[str, Any], ts_recv_ns: int) -> None:
        """MarketPool listener: runs after the book tracker applied the event."""
        kind = event.get("event_type") or event.get("type")
        if kind == "book":
            slot = self.slots.get(str(event.get("asset_id") or ""))
            if slot is None:
                return
            tick = to_price(event.get("tick_size"))
            min_size = to_decimal(event.get("min_order_size"))
            if tick is not None and tick > 0:
                slot.tick = tick
            if min_size is not None and min_size > 0:
                slot.min_size = min_size
            self._set_rules(slot)
            self._book_changed(slot, ts_recv_ns)
        elif kind == "price_change":
            changes = event.get("price_changes")
            tokens = {str(c.get("asset_id") or "") for c in changes or [] if isinstance(c, dict)}
            for token in tokens:
                slot = self.slots.get(token)
                if slot is not None:
                    self._book_changed(slot, ts_recv_ns)
        elif kind == "last_trade_price":
            slot = self.slots.get(str(event.get("asset_id") or ""))
            price, size = to_price(event.get("price")), to_decimal(event.get("size"))
            if slot is not None and price is not None and size is not None and size > 0:
                self.venue.on_trade(TradePrint(slot.ref, price, size, None, None, ts_recv_ns))
        elif kind == "tick_size_change":
            slot = self.slots.get(str(event.get("asset_id") or ""))
            if slot is not None:
                # The tracker dropped the book; the new tick comes with the next snapshot.
                slot.tick = None
                self._set_rules(slot)
                slot.dirty = True

    def _book_changed(self, slot: Slot, ts: int) -> None:
        book = self._snapshot(slot, ts)
        if book is None:
            return
        self.venue.on_book(book)
        slot.fair.update(book)
        self._mark(slot)
        slot.dirty = True

    def _mark(self, slot: Slot) -> None:
        """Positions are valued at the current microprice; quotes follow the slower EWMA."""
        if slot.fair.micro is not None:
            self.marks[slot.cand.token] = slot.fair.micro

    def _snapshot(self, slot: Slot, ts: int) -> BookSnapshot | None:
        book = self.pool.tracker.books.get(slot.cand.token)
        if book is None or not book.ready:
            return None
        bids, asks = book.levels()
        server_ns = book.last_server_ts_ms * 1_000_000 if book.last_server_ts_ms else None
        return BookSnapshot(
            slot.ref,
            [BookLevel(p, s) for p, s in bids],
            [BookLevel(p, s) for p, s in asks],
            server_ns,
            ts,
        )

    # ------------------------------------------------------------------ quoting

    async def run(self) -> None:
        while True:
            await self.step()
            await asyncio.sleep(STEP_S)

    async def step(self) -> None:
        now = self.clock()
        self.venue.advance(now)
        await self._roll_day(now)
        value = self.value()
        if self.loss_guard.update(ns_to_date(now), value) and not self.day.halted:
            self.day.halted = True
            log.warning("minibot_daily_loss_limit", value=round(value, 2))
            for slot in self.slots.values():
                if slot.phase is not Phase.PULLED:
                    await self._pull(slot, Phase.HALTED, "daily_loss_limit")
            self._report(self.reporter.loss_limit(value, value - self.day.start_value))
        for slot in list(self.slots.values()):
            await self._step_slot(slot, now)
        if now - self._last_reward_ns >= REWARD_SAMPLE_NS:
            self._last_reward_ns = now
            self._sample_minute(now)

    async def _step_slot(self, slot: Slot, now: int) -> None:
        cand, token = slot.cand, slot.cand.token
        if slot.phase is Phase.PULLED:
            return
        if now >= cand.start_ns - int(self.cfg.quoting.pull_before_start_min * NS_PER_MIN):
            await self._pull(slot, Phase.PULLED, "before_kickoff")
            return
        if self.day.halted:
            await self._pull(slot, Phase.HALTED, "daily_loss_limit")
            return
        if now > self.meta_fresh_until_ns:
            await self._pull(slot, Phase.WAITING, "stale_metadata")
            return
        if slot.tick is None or slot.min_size is None:
            await self._pull(slot, Phase.WAITING, "tick_change")
            return
        book = self._snapshot(slot, now) if self.pool.asset_live(token) else None
        if book is None:
            await self._pull(slot, Phase.WAITING, "no_book")
            return
        slot.fair.update(book)  # a quiet book still moves the EWMA with time
        self._mark(slot)
        if slot.fair.jumped(float(slot.tick), self.params.jump_ticks):
            slot.pause_until_ns = now + int(self.params.jump_cooldown_s * NS_PER_S)
            await self._pull(slot, Phase.PAUSED, "jump")
            return
        if slot.phase is Phase.PAUSED and now < slot.pause_until_ns:
            return
        interval_ns = int(self.cfg.quoting.requote_interval_s * NS_PER_S)
        if (
            slot.phase is Phase.QUOTING
            and not slot.dirty
            and now - slot.last_quote_ns < interval_ns
        ):
            return
        desired = self._desired(slot, book)
        await self._reconcile(slot, desired)
        slot.phase, slot.reason = (Phase.QUOTING, "") if desired else (Phase.WAITING, "no_quotes")
        slot.dirty, slot.last_quote_ns = False, now
        if desired and not cand.reviewed:
            self.unreviewed_today.add(token)

    def _desired(self, slot: Slot, book: BookSnapshot) -> list[Quote]:
        fair = slot.fair.value
        if fair is None or slot.tick is None or slot.min_size is None:
            return []
        risk, rewards = self.cfg.risk, slot.cand.rewards
        min_rewards = rewards.min_size if rewards else None
        bid_size, ask_size = (
            order_size(
                cost_per_share=Decimal(str(round(cost, 4))),
                min_order_size=slot.min_size,
                rewards_min_size=min_rewards,
                max_order_usd=risk.max_order_usd,
            )
            for cost in (fair, 1 - fair)
        )
        holding = self.venue.holdings.get(slot.cand.token)
        return compute_quotes(
            book=book,
            fair=fair,
            tick=slot.tick,
            rewards=rewards,
            position=holding.net if holding else Decimal(0),
            # One share cap both ways, sized on the dearer side: ≤ max_position_usd at cost.
            max_position=max_position_shares(risk.max_position_usd, max(fair, 1 - fair)),
            bid_size=bid_size,
            ask_size=ask_size,
            params=self.params,
        )

    async def _reconcile(self, slot: Slot, desired: list[Quote]) -> None:
        token, tick, min_size = slot.cand.token, slot.tick, slot.min_size
        if tick is None or min_size is None:
            return
        rewards_min = Decimal(str(slot.cand.rewards.min_size)) if slot.cand.rewards else None
        open_orders = self.venue.open_orders(token)
        for side in (Side.BUY, Side.SELL):
            want = next((q for q in desired if q.side is side), None)
            have = [o for o in open_orders if o.side is side]
            if want is not None and len(have) == 1:
                # Keep the queue place while the price is close and enough size is left
                # (still eligible for rewards if the order was sized for them).
                floor = min_size
                if rewards_min is not None and want.size >= rewards_min:
                    floor = max(floor, rewards_min)
                current = Quote(side, have[0].price, want.size)
                if have[0].remaining >= floor and not needs_requote(
                    current, want, tick, self.params.requote_ticks
                ):
                    continue
            if have:
                await self.venue.cancel([o.order_id for o in have])
                for order in have:
                    self._record("cancel", token, {"order_id": order.order_id})
            if want is None:
                continue
            try:
                order_id = await self.venue.place_post_only(slot.ref, side, want.price, want.size)
            except PaperRejected as exc:
                slot.dirty = True
                self._record("reject", token, {"side": side.value, "code": exc.code})
                continue
            self._record(
                "order",
                token,
                {
                    "order_id": order_id,
                    "side": side.value,
                    "price": str(want.price),
                    "size": str(want.size),
                    "fair": round(slot.fair.value or 0.0, 5),
                },
            )

    async def _pull(self, slot: Slot, phase: Phase, reason: str) -> None:
        orders = self.venue.open_orders(slot.cand.token)
        if orders:
            await self.venue.cancel([o.order_id for o in orders])
            self._record("pull", slot.cand.token, {"reason": reason, "orders": len(orders)})
        if slot.phase is not phase or slot.reason != reason:
            log.info("minibot_market_phase", title=slot.cand.title, phase=phase, reason=reason)
        slot.phase, slot.reason = phase, reason

    def drop_all(self) -> None:
        """Start and stop (CLAUDE.md, rule 9): no paper order outlives the process."""
        self.venue.drop_all()
        for slot in self.slots.values():
            if slot.phase is not Phase.PULLED:
                slot.phase, slot.reason = Phase.WAITING, "no_book"

    # ------------------------------------------------------------------ accounting

    def value(self) -> float:
        return self.venue.value(self.marks)

    def _on_fill(self, fill: PaperFill) -> None:
        token = fill.ref.instrument_id
        known = self.known.get(token)
        notional = float(fill.price * fill.size)
        self.day.fills += 1
        self.day.volume_usd += notional
        if known is not None:
            market = self.day.market(known)
            market.fills += 1
            market.volume_usd += notional
        slot = self.slots.get(token)
        if slot is not None:
            slot.dirty = True
        self._record(
            "fill",
            token,
            {
                "order_id": fill.order_id,
                "side": fill.side.value,
                "price": str(fill.price),
                "size": str(fill.size),
                "rebate": round(fill.rebate, 6),
            },
        )
        log.info(
            "paper_fill",
            title=known.title if known else token[:12],
            side=fill.side.value,
            price=str(fill.price),
            size=str(fill.size),
        )

    def _on_order_event(self, event: PaperEvent) -> None:
        if event.kind != "reject":
            return
        token = event.order.ref.instrument_id
        self._record("reject", token, {"code": event.reason, "at_activation": True})
        slot = self.slots.get(token)
        if slot is not None:
            slot.dirty = True

    def _sample_minute(self, now: int) -> None:
        """Once a minute: time in the book and the rewards estimate (docs/api_notes.md §9)."""
        for slot in self.slots.values():
            if slot.phase is not Phase.QUOTING:
                continue
            book = self._snapshot(slot, now)
            known = self.known.get(slot.cand.token)
            if book is None or known is None:
                continue
            live = [o for o in self.venue.open_orders(slot.cand.token) if o.active]
            bids = [(float(o.price), float(o.remaining)) for o in live if o.side is Side.BUY]
            asks = [(float(o.price), float(o.remaining)) for o in live if o.side is Side.SELL]
            market = self.day.market(known)
            if bids and asks:
                market.quoted_min += 1
            rewards = slot.cand.rewards
            if rewards is None:
                continue
            sample = reward_math.sample(
                book, bids, asks, max_spread=rewards.max_spread, min_size=rewards.min_size
            )
            if sample is None or sample.share <= 0:
                continue
            usd = sample.share * rewards.daily_rate / 1440
            market.rewards_usd += usd
            self.day.rewards_usd += usd
            self._record(
                "rewards", slot.cand.token, {"share": round(sample.share, 6), "usd": round(usd, 6)}
            )

    # ------------------------------------------------------------------ settlement

    async def settlement_loop(self, interval_s: float = 600.0) -> None:
        while True:
            await self.settle_due()
            await asyncio.sleep(interval_s)

    async def settle_due(self) -> None:
        now = self.clock()
        for token, holding in list(self.venue.holdings.items()):
            known = self.known.get(token)
            if known is None or now < known.start_ns + SETTLE_AFTER_START_NS:
                continue
            if holding.long == 0 and holding.short == 0:
                self._forget(token)
                continue
            try:
                market = await self.fetch_market(known.market_id)
            except Exception as exc:  # network or format: try again next round
                log.warning("minibot_settlement_fetch_failed", error=repr(exc))
                continue
            payouts = final_payouts(market, known.yes_index)
            if payouts is None:
                continue
            pnl = float(self.venue.settle(token, payouts[0], payouts[1]))
            outcome = {Decimal(1): "yes", Decimal(0): "no"}.get(payouts[0], str(payouts[0]))
            settlement = Settlement(known.title, known.label, outcome, round(pnl, 2))
            self.day.settlements.append(settlement)
            self.day.settled_pnl += pnl
            self._record("settle", token, asdict(settlement))
            log.info("paper_settled", **asdict(settlement))
            self._forget(token)
            self.save_state()
            self._report(self.reporter.settled(settlement))

    # ------------------------------------------------------------------ day, state, status

    def _today(self) -> str:
        return ns_to_date(self.clock()).isoformat()

    def _closed(self, stats: DayStats) -> ClosedDay:
        return ClosedDay(
            stats=stats,
            end_value=self.value(),
            rebates_total=self.venue.rebates,
            deposit=self.deposit,
            unreviewed=len(self.unreviewed_today),
        )

    async def _roll_day(self, now: int) -> None:
        today = ns_to_date(now).isoformat()
        if today == self.day.day:
            return
        closed = self._closed(self.day)
        self.day = DayStats(
            day=today, start_value=closed.end_value, rebates_at_start=closed.rebates_total
        )
        self.unreviewed_today = {
            t for t, s in self.slots.items() if s.phase is Phase.QUOTING and not s.cand.reviewed
        }
        self.save_state()
        log.info("minibot_day_closed", day=closed.stats.day, value=round(closed.end_value, 2))
        self._report(self.reporter.day_closed(closed))

    def _report(self, report: Coroutine[Any, Any, None]) -> None:
        """Reports go out in the background: a slow Telegram never stalls quotes or pulls."""
        task = asyncio.get_running_loop().create_task(report)
        self._reports.add(task)
        task.add_done_callback(self._reports.discard)

    async def wait_reports(self, timeout_s: float = 30.0) -> None:
        """Let reports in flight finish (shutdown, tests)."""
        if self._reports:
            await asyncio.wait(set(self._reports), timeout=timeout_s)

    def status(self, extra: dict[str, Any] | None = None) -> Status:
        now = self.clock()
        markets: list[MarketStatus] = []
        for slot in sorted(self.slots.values(), key=lambda s: (s.cand.start_ns, s.cand.token)):
            cand = slot.cand
            orders = self.venue.open_orders(cand.token)
            holding = self.venue.holdings.get(cand.token)
            markets.append(
                MarketStatus(
                    token=cand.token,
                    title=cand.title,
                    label=cand.label,
                    league=cand.league,
                    start_ns=cand.start_ns,
                    phase=slot.phase.value,
                    reason=slot.reason,
                    fair=round(slot.fair.value, 4) if slot.fair.value is not None else None,
                    bid=next((str(o.price) for o in orders if o.side is Side.BUY), None),
                    ask=next((str(o.price) for o in orders if o.side is Side.SELL), None),
                    position=str(holding.net if holding else 0),
                    rewards_daily=cand.rewards.daily_rate if cand.rewards else None,
                    reviewed=cand.reviewed,
                )
            )
        unsettled = sum(
            1
            for token, h in self.venue.holdings.items()
            if (h.long or h.short) and token in self.known and self.known[token].start_ns <= now
        )
        return Status(
            ts_ns=now,
            day=self.day.day,
            deposit=self.deposit,
            value=round(self.value(), 4),
            day_start_value=self.day.start_value,
            cash=float(self.venue.cash),
            locked=float(self.venue.locked_cash),
            rebates_total=round(self.venue.rebates, 6),
            rewards_day=round(self.day.rewards_usd, 6),
            fills_day=self.day.fills,
            volume_day=round(self.day.volume_usd, 2),
            halted=self.day.halted,
            unsettled=unsettled,
            markets=markets,
            extra=extra or {},
        )

    def write_status(self, extra: dict[str, Any] | None = None) -> Status:
        status = self.status(extra)
        _write_json(self.status_path, status.to_dict())
        return status

    def save_state(self) -> None:
        state = {
            "saved_ns": self.clock(),
            "cash": str(self.venue.cash),
            "deposit": self.deposit,
            "rebates": self.venue.rebates,
            "holdings": {
                t: {"long": str(h.long), "short": str(h.short), "cost": str(h.cost)}
                for t, h in self.venue.holdings.items()
                if h.long or h.short
            },
            "known": {t: asdict(k) for t, k in self.known.items()},
            "marks": self.marks,
            "day": self.day.to_dict(),
            "unreviewed_today": sorted(self.unreviewed_today),
        }
        _write_json(self.state_path, state)

    def load_state(self) -> ClosedDay | None:
        """Restore the paper portfolio of an earlier run (open orders are never restored).

        Returns the saved day when it is already over, for its (late) daily report.
        Raises RuntimeError on a corrupt file: starting over would silently reset P&L.
        """
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"unreadable paper state {self.state_path}: {exc!r}") from exc
        self.venue.cash = Decimal(state["cash"])
        self.deposit = float(state["deposit"])
        self.venue.rebates = float(state.get("rebates", 0.0))
        for token, h in state.get("holdings", {}).items():
            holding = self.venue.holding(token)
            holding.long, holding.short = Decimal(h["long"]), Decimal(h["short"])
            holding.cost = Decimal(h["cost"])
        self.known = {t: Known(**k) for t, k in state.get("known", {}).items()}
        self.marks = {t: float(v) for t, v in state.get("marks", {}).items()}
        self.unreviewed_today = set(state.get("unreviewed_today", []))
        saved = DayStats.from_dict(state["day"])
        today = self._today()
        if saved.day == today:
            self.day = saved
            self.loss_guard = DailyLossGuard(
                self.cfg.risk.daily_loss_limit_usd,
                day=date.fromisoformat(today),
                start_value=saved.start_value,
                tripped=saved.halted,
            )
            return None
        closed = self._closed(saved)
        self.day = DayStats(
            day=today, start_value=closed.end_value, rebates_at_start=closed.rebates_total
        )
        self.unreviewed_today = set()
        return closed

    def _record(self, kind: str, token: str, info: dict[str, Any]) -> None:
        slot = self.slots.get(token)
        self.sink.write(
            Record(
                ts_recv_ns=self.clock(),
                source=Source.PAPER,
                kind=Kind.CONTROL,
                event_type=kind,
                market=slot.ref.market_id if slot else None,
                asset_id=token,
                payload=json.dumps(info, default=str),
            )
        )


def _known(cand: Candidate) -> Known:
    return Known(
        token=cand.token,
        market_id=cand.market.market_id,
        condition_id=cand.market.condition_id,
        yes_index=cand.yes_index,
        start_ns=cand.start_ns,
        title=cand.title,
        label=cand.label,
        league=cand.league,
        reviewed=cand.reviewed,
    )


def final_payouts(market: dict[str, Any], yes_index: int) -> tuple[Decimal, Decimal] | None:
    """(Yes, No) payout per share of a closed market; None while it is open (api_notes §11)."""
    if market.get("closed") is not True:
        return None
    raw = market.get("outcomePrices")
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None
    if not isinstance(prices, list) or len(prices) != 2:
        return None
    yes, no = to_decimal(prices[yes_index]), to_decimal(prices[1 - yes_index])
    if yes is None or no is None or not (0 <= yes <= 1 and 0 <= no <= 1):
        return None
    if yes + no != 1:
        return None  # not a final payout (e.g. still last traded prices): wait
    return yes, no


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, default=str, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
