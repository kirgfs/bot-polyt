"""Shared fixtures of the mini-bot tests: Gamma-shaped soccer events, books, a fake pool."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from polybot.core.config import MiniBotConfig
from polybot.core.timeutil import NS_PER_S, ns_to_datetime
from polybot.execution.paper import PaperVenue
from polybot.minibot.engine import Engine
from polybot.minibot.report import Reporter
from polybot.venues.polymarket.markets import ParseIssues, PmEvent, PmMarket, parse_event
from polybot.venues.polymarket.orderbook import BookTracker
from tests.conftest import ListWriter

MIN = 60 * NS_PER_S
H = 60 * MIN
T0 = int(datetime(2026, 9, 26, 12, 0, tzinfo=UTC).timestamp()) * NS_PER_S
SIDES = ("Inter", "Draw", "Milan")

DESCRIPTION = (
    "In the upcoming Serie A game, scheduled for September 27, 2026 at 18:45 UTC:\n"
    "If {side} wins, this market will resolve to Yes. Otherwise, it resolves to No.\n"
    "The result is determined after 90 minutes of play plus stoppage time."
)


def mini_config(**overrides: Any) -> MiniBotConfig:
    data: dict[str, Any] = {"leagues": ["serie-a"], "telegram": {"status_every_h": 6}}
    data.update(overrides)
    return MiniBotConfig.model_validate(data)


def raw_event(
    event_id: str,
    start_ns: int,
    *,
    league: str = "serie-a",
    home: str = "Inter",
    away: str = "Milan",
    rewards: bool = True,
    outcomes: tuple[str, str] = ("Yes", "No"),
) -> dict[str, Any]:
    stamp = ns_to_datetime(start_ns).strftime("%Y-%m-%d %H:%M:%S+00")
    markets = []
    for idx, side in enumerate((home, "Draw", away)):
        market: dict[str, Any] = {
            "id": f"{event_id}{idx}",
            "question": f"Will {side} win?" if side != "Draw" else f"{home} vs. {away}: draw?",
            "conditionId": "0x" + f"{event_id}{idx}".rjust(64, "0"),
            "slug": f"sea-{event_id}-{idx}",
            "outcomes": json.dumps(list(outcomes)),
            "clobTokenIds": json.dumps([f"{event_id}{idx}1", f"{event_id}{idx}2"]),
            "sportsMarketType": "moneyline",
            "gameStartTime": stamp,
            "active": True,
            "closed": False,
            "acceptingOrders": True,
            "enableOrderBook": True,
            "orderPriceMinTickSize": 0.01,
            "orderMinSize": 5,
            "description": DESCRIPTION.format(side=side),
            "feeSchedule": {"rate": 0.02, "exponent": 1, "rebateRate": 0.25},
        }
        if rewards:
            market |= {
                "rewardsMinSize": 20,
                "rewardsMaxSpread": 3.5,
                "clobRewards": [{"rewardsDailyRate": 10 + idx}],
            }
        markets.append(market)
    return {
        "id": event_id,
        "slug": f"sea-{home.lower()}-{away.lower()}-{event_id}",
        "title": f"{home} vs. {away}",
        "homeTeamName": home,
        "awayTeamName": away,
        "tags": [{"slug": "soccer"}, {"slug": league}, {"slug": "games"}],
        "markets": markets,
    }


def pairs(*raws: dict[str, Any]) -> list[tuple[PmEvent, PmMarket]]:
    out = []
    for raw in raws:
        event = parse_event(raw, "soccer", ParseIssues())
        out += [(event, market) for market in event.markets]
    return out


def book_event(
    token: str,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
    *,
    tick: str = "0.01",
    min_size: str = "5",
) -> dict[str, Any]:
    return {
        "event_type": "book",
        "market": "0x" + "ab" * 32,
        "asset_id": token,
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
        "hash": f"h-{token}",
        "timestamp": "1790000000000",
        "tick_size": tick,
        "min_order_size": min_size,
    }


def trade_event(token: str, price: str, size: str) -> dict[str, Any]:
    return {
        "event_type": "last_trade_price",
        "market": "0x" + "ab" * 32,
        "asset_id": token,
        "price": price,
        "size": size,
        "side": "SELL",
        "fee_rate_bps": "0",
        "timestamp": "1790000000000",
    }


class Clock:
    def __init__(self, now: int = T0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class FakePool:
    """What the engine uses of MarketPool: the book tracker and liveness."""

    def __init__(self) -> None:
        self.tracker = BookTracker()
        self.dead: set[str] = set()

    def set_assets(self, tokens: set[str]) -> None:
        for token in tokens - set(self.tracker.books):
            self.tracker.expect_snapshot(token)

    def asset_live(self, asset: str) -> bool:
        book = self.tracker.books.get(asset)
        return asset not in self.dead and book is not None and book.ready

    def feed(self, engine: Engine, event: dict[str, Any], ts: int) -> None:
        """Like MarketPool.on_frame: the tracker first, then the listener."""
        self.tracker.on_event(event, ts)
        engine.on_ws_event(event, ts)


class Collect:
    """Notifier that keeps messages."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send(self, text: str, *, silent: bool = False) -> None:
        self.messages.append((text, silent))

    def texts(self) -> list[str]:
        return [text for text, _ in self.messages]


def make_engine(
    tmp_path: Path,
    clock: Clock,
    cfg: MiniBotConfig | None = None,
    markets: dict[str, dict[str, Any]] | None = None,
) -> tuple[Engine, FakePool, Collect, ListWriter]:
    cfg = cfg or mini_config()
    pool, notifier, sink = FakePool(), Collect(), ListWriter()
    venue = PaperVenue(cash=Decimal(str(cfg.risk.deposit_usd)), latency_ns=100_000_000, clock=clock)
    served = markets if markets is not None else {}

    async def fetch_market(market_id: str) -> dict[str, Any]:
        return served[market_id]

    engine = Engine(
        cfg,
        venue=venue,
        pool=pool,  # type: ignore[arg-type]
        sink=sink,
        reporter=Reporter(notifier, cfg, tmp_path / "reports"),
        fetch_market=fetch_market,
        state_dir=tmp_path / "state",
        clock=clock,
    )
    return engine, pool, notifier, sink
