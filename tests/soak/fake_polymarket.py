"""Local fake Polymarket for the recorder soak test (no external network).

Serves what `polybot record` talks to: geoblock, Gamma tags and `/events/keyset`, CLOB REST
(`/time`, `POST /books`, `/clob-markets/{cid}`, `/rewards/markets/current`), the market
channel WS and the Sports WS. Payload shapes follow docs/api_notes.md §10–11; sizes are
deliberately realistic (long rule texts, side markets on every event, deep books), since
the point is memory under production-like load.

Run: python -m tests.soak.fake_polymarket --http-port 8801 --ws-port 8802 --sports-port 8803
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

TAG_IDS = {"tennis": 864, "soccer": 100350, "basketball": 745, "games": 100639}
H = 3600.0


@dataclass(frozen=True)
class Scale:
    matches: dict[str, int]  # open match events per sport
    futures: dict[str, int]  # open non-match events (outrights) per sport
    side_markets: int  # non-moneyline markets per match event
    levels: int  # book depth per side
    rate: float  # price_change frames per second over all subscribed assets
    desc_bytes: int  # rule text length per market
    churn_every_s: float  # replace some match events this often...
    churn_step: int  # ...this many per sport


def digits(*parts: object, n: int = 77) -> str:
    seed = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return str(int(seed, 16))[:n].rjust(n, "1")


def hex64(*parts: object) -> str:
    return "0x" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def gamma_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S+00")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def rule_text(n: int) -> str:
    base = (
        "This market will resolve according to the official result of the match. If the "
        "match is postponed beyond the scheduled window, cancelled or abandoned, the market "
        "resolves 50-50 unless a winner is declared by the governing body. "
    )
    return (base * (n // len(base) + 1))[:n]


class World:
    """Synthetic events, their tokens and the order books behind them."""

    def __init__(self, scale: Scale, seed: int) -> None:
        self.scale = scale
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.version = 0
        self.next_id = 1_000_000
        self.events: dict[str, list[dict[str, Any]]] = {s: [] for s in scale.matches}
        self.cid_of: dict[str, str] = {}  # token -> condition id
        self.last_churn = time.time()
        self.pages: dict[tuple[str, int], str] = {}
        now = time.time()
        for sport, n in scale.matches.items():
            for _ in range(n):
                self.events[sport].append(
                    self._match(sport, now + self.rng.uniform(-5 * H, 7 * 24 * H))
                )
            for _ in range(scale.futures[sport]):
                self.events[sport].append(self._future(sport))

    # ------------------------------------------------------------------ generation

    def _new_id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def _market(
        self,
        event_id: str,
        idx: int,
        question: str,
        outcomes: list[str],
        market_type: str | None,
        start: float | None,
    ) -> dict[str, Any]:
        tokens = [digits(event_id, idx, k) for k in range(len(outcomes))]
        cid = hex64(event_id, idx)
        for token in tokens:
            self.cid_of[token] = cid
        now = time.time()
        market: dict[str, Any] = {
            "id": str(int(event_id) * 100 + idx),
            "question": question,
            "conditionId": cid,
            "slug": f"m-{event_id}-{idx}",
            "resolutionSource": "https://www.example.org/results",
            "endDate": iso((start or now) + 3 * H),
            "liquidity": f"{self.rng.uniform(100, 50000):.4f}",
            "startDate": iso(now - 24 * H),
            "image": f"https://polymarket-upload.s3.us-east-2.amazonaws.com/{event_id}.png",
            "icon": f"https://polymarket-upload.s3.us-east-2.amazonaws.com/{event_id}-icon.png",
            "description": rule_text(self.scale.desc_bytes),
            "outcomes": json.dumps(outcomes),
            "outcomePrices": json.dumps(["0.5"] * len(outcomes)),
            "volume": f"{self.rng.uniform(0, 1e6):.6f}",
            "active": True,
            "closed": False,
            "marketMakerAddress": "",
            "createdAt": iso(now - 48 * H),
            "updatedAt": iso(now),
            "new": False,
            "featured": False,
            "archived": False,
            "restricted": True,
            "groupItemTitle": outcomes[0],
            "questionID": hex64("q", event_id, idx),
            "enableOrderBook": True,
            "orderPriceMinTickSize": 0.01,
            "orderMinSize": 5,
            "volumeNum": self.rng.uniform(0, 1e6),
            "liquidityNum": self.rng.uniform(100, 50000),
            "endDateIso": iso((start or now) + 3 * H)[:10],
            "startDateIso": iso(now - 24 * H)[:10],
            "volume24hr": self.rng.uniform(0, 1e5),
            "volume1wk": self.rng.uniform(0, 1e5),
            "volume1mo": self.rng.uniform(0, 1e5),
            "volume1yr": self.rng.uniform(0, 1e5),
            "clobTokenIds": json.dumps(tokens),
            "umaBond": "500",
            "umaReward": "5",
            "volume24hrClob": self.rng.uniform(0, 1e5),
            "volumeClob": self.rng.uniform(0, 1e6),
            "liquidityClob": self.rng.uniform(100, 50000),
            "acceptingOrders": True,
            "negRisk": False,
            "ready": False,
            "funded": False,
            "acceptingOrdersTimestamp": iso(now - 24 * H),
            "cyom": False,
            "competitive": self.rng.random(),
            "pagerDutyNotificationEnabled": False,
            "approved": True,
            "clobRewards": [
                {
                    "id": str(idx),
                    "conditionId": cid,
                    "assetAddress": "0x" + "ab" * 20,
                    "rewardsAmount": 0,
                    "rewardsDailyRate": 5,
                    "startDate": iso(now - 24 * H)[:10],
                    "endDate": "2500-12-31",
                }
            ],
            "rewardsMinSize": 50,
            "rewardsMaxSpread": 3.5,
            "spread": 0.01,
            "oneDayPriceChange": 0.01,
            "lastTradePrice": 0.5,
            "bestBid": 0.49,
            "bestAsk": 0.51,
            "automaticallyActive": True,
            "clearBookOnStart": True,
            "secondsDelay": 3,
            "feesEnabled": True,
        }
        if market_type is not None and start is not None:
            market["sportsMarketType"] = market_type
            market["gameStartTime"] = gamma_time(start)
        return market

    def _match(self, sport: str, start: float) -> dict[str, Any]:
        event_id = self._new_id()
        home, away = f"Home {event_id}", f"Away {event_id}"
        markets: list[dict[str, Any]] = []
        if sport == "soccer":
            for idx, side in enumerate((home, "Draw", away)):
                markets.append(
                    self._market(
                        event_id, idx, f"Will {side} win?", ["Yes", "No"], "moneyline", start
                    )
                )
        else:
            markets.append(
                self._market(event_id, 0, f"{home} vs. {away}", [home, away], "moneyline", start)
            )
        for k in range(self.scale.side_markets):
            kind = ("totals", "spreads", "both_teams_to_score")[k % 3]
            markets.append(
                self._market(
                    event_id,
                    10 + k,
                    f"{home} vs. {away}: {kind} {k}",
                    ["Over", "Under"],
                    kind,
                    start,
                )
            )
        league = {"tennis": "atp", "soccer": "epl", "basketball": "nba"}[sport]
        return {
            "id": event_id,
            "ticker": f"{league}-{event_id}",
            "slug": f"{league}-{home.lower().replace(' ', '')}-{away.lower().replace(' ', '')}",
            "title": f"{home} vs. {away}",
            "description": rule_text(self.scale.desc_bytes),
            "startDate": iso(start - 48 * H),
            "endDate": iso(start + 3 * H),
            "image": f"https://polymarket-upload.s3.us-east-2.amazonaws.com/{event_id}.png",
            "active": True,
            "closed": False,
            "live": False,
            "ended": False,
            "gameId": int(event_id) + 7,
            "sportsradarMatchId": str(60_000_000 + int(event_id)),
            "homeTeamName": home,
            "awayTeamName": away,
            "tags": [
                {"id": str(TAG_IDS[sport]), "label": sport.title(), "slug": sport},
                {"id": str(TAG_IDS["games"]), "label": "Games", "slug": "games"},
            ],
            "markets": markets,
        }

    def _future(self, sport: str) -> dict[str, Any]:
        event_id = self._new_id()
        markets = [
            self._market(
                event_id, idx, f"Will team {idx} win the league?", ["Yes", "No"], None, None
            )
            for idx in range(30)
        ]
        return {
            "id": event_id,
            "slug": f"{sport}-winner-{event_id}",
            "title": f"{sport.title()} winner {event_id}",
            "description": rule_text(self.scale.desc_bytes),
            "active": True,
            "closed": False,
            "tags": [{"id": str(TAG_IDS[sport]), "label": sport.title(), "slug": sport}],
            "markets": markets,
        }

    # ------------------------------------------------------------------ Gamma views

    def maybe_churn(self) -> None:
        """Replace the oldest match events: resolved games leave, new ones enter the horizon."""
        with self.lock:
            now = time.time()
            if now - self.last_churn < self.scale.churn_every_s:
                return
            self.last_churn = now
            for sport, events in self.events.items():
                matches = [e for e in events if "homeTeamName" in e]
                for old in matches[: self.scale.churn_step]:
                    events.remove(old)
                for _ in range(self.scale.churn_step):
                    start = now + self.rng.uniform(1 * H, 48 * H)
                    events.append(self._match(sport, start))
            self.version += 1
            self.pages.clear()

    def keyset_page(self, tag_id: int, offset: int, limit: int) -> str:
        self.maybe_churn()
        sport = next(s for s, t in TAG_IDS.items() if t == tag_id)
        with self.lock:
            cached = self.pages.get((sport, offset))
            if cached is not None:
                return cached
            events = self.events.get(sport, [])
            chunk = events[offset : offset + limit]
            body: dict[str, Any] = {"events": chunk}
            if offset + limit < len(events):
                body["next_cursor"] = f"c{self.version}-{offset + limit}"
            text = json.dumps(body)
            self.pages[(sport, offset)] = text
            return text


class Book:
    """Integer-cent book (tick 0.01) that emits consistent `price_change` messages."""

    __slots__ = ("asks", "bids")

    def __init__(self, rng: random.Random, levels: int) -> None:
        mid = rng.randint(15, 85)
        self.bids = {c: rng.randint(5, 5000) for c in range(max(1, mid - levels), mid)}
        self.asks = {c: rng.randint(5, 5000) for c in range(mid + 1, min(99, mid + levels) + 1)}

    def top(self) -> tuple[str, str]:
        bid = f"{max(self.bids) / 100:.2f}" if self.bids else "0"
        ask = f"{min(self.asks) / 100:.2f}" if self.asks else "1"
        return bid, ask

    def mutate(self, rng: random.Random) -> tuple[str, int, int]:
        side = "BUY" if rng.random() < 0.5 else "SELL"
        levels = self.bids if side == "BUY" else self.asks
        roll = rng.random()
        if levels and roll < 0.7:
            price = rng.choice(list(levels))
            size = rng.randint(5, 5000)
        elif levels and roll < 0.85 and len(levels) > 2:
            price, size = rng.choice(list(levels)), 0
        else:
            lo, hi = (
                (1, min(self.asks, default=100) - 1)
                if side == "BUY"
                else (
                    max(self.bids, default=0) + 1,
                    99,
                )
            )
            if lo > hi:
                return self.mutate(rng)
            price, size = rng.randint(lo, hi), rng.randint(5, 5000)
        if size:
            levels[price] = size
        else:
            levels.pop(price, None)
        return side, price, size

    def snapshot(self, asset: str, market: str) -> dict[str, Any]:
        return {
            "event_type": "book",
            "market": market,
            "asset_id": asset,
            "bids": [
                {"price": f"{c / 100:.2f}", "size": f"{s}.00"} for c, s in sorted(self.bids.items())
            ],
            "asks": [
                {"price": f"{c / 100:.2f}", "size": f"{s}.00"}
                for c, s in sorted(self.asks.items(), reverse=True)
            ],
            "hash": hashlib.sha1(repr((self.bids, self.asks)).encode()).hexdigest(),
            "timestamp": str(int(time.time() * 1000)),
            "tick_size": "0.01",
            "last_trade_price": "0.50",
        }


class MarketChannel:
    def __init__(self, world: World, seed: int) -> None:
        self.world = world
        self.rng = random.Random(seed)
        self.books: dict[str, Book] = {}
        self.conns: dict[ServerConnection, set[str]] = {}
        self.frames_sent = 0

    def total_assets(self) -> int:
        return sum(len(a) for a in self.conns.values())

    async def _snapshots(self, ws: ServerConnection, assets: list[str]) -> None:
        for i in range(0, len(assets), 50):
            batch = []
            for asset in assets[i : i + 50]:
                book = self.books.setdefault(asset, Book(self.rng, self.world.scale.levels))
                batch.append(book.snapshot(asset, self.world.cid_of.get(asset, hex64(asset))))
            await ws.send(json.dumps(batch))
            self.frames_sent += 1

    async def handler(self, ws: ServerConnection) -> None:
        assets: set[str] = set()
        self.conns[ws] = assets
        emitter = asyncio.create_task(self._emit(ws, assets))
        try:
            async for raw in ws:
                if raw == "PING":
                    await ws.send("PONG")
                    continue
                msg = json.loads(raw)
                ids = [str(a) for a in msg.get("assets_ids", [])]
                if msg.get("type") == "market" or msg.get("operation") == "subscribe":
                    assets.update(ids)
                    await self._snapshots(ws, ids)
                elif msg.get("operation") == "unsubscribe":
                    assets.difference_update(ids)
        except ConnectionClosed:
            pass
        finally:
            emitter.cancel()
            self.conns.pop(ws, None)

    async def _emit(self, ws: ServerConnection, assets: set[str]) -> None:
        tick_s, carry = 0.05, 0.0
        while True:
            await asyncio.sleep(tick_s)
            total = self.total_assets()
            if not assets or not total:
                continue
            carry += self.world.scale.rate * tick_s * len(assets) / total
            n, carry = int(carry), carry - int(carry)
            pool = list(assets)
            for _ in range(n):
                asset = self.rng.choice(pool)
                book = self.books.get(asset)
                if book is None:
                    continue
                market = self.world.cid_of.get(asset, hex64(asset))
                if self.rng.random() < 0.05:
                    frame: dict[str, Any] = {
                        "event_type": "last_trade_price",
                        "market": market,
                        "asset_id": asset,
                        "price": book.top()[0] if book.bids else "0.50",
                        "size": f"{self.rng.randint(5, 500)}",
                        "side": "BUY",
                        "fee_rate_bps": "0",
                        "timestamp": str(int(time.time() * 1000)),
                    }
                else:
                    side, price, size = book.mutate(self.rng)
                    best_bid, best_ask = book.top()
                    frame = {
                        "event_type": "price_change",
                        "market": market,
                        "price_changes": [
                            {
                                "asset_id": asset,
                                "price": f"{price / 100:.2f}",
                                "size": f"{size}.00" if size else "0",
                                "side": side,
                                "hash": hashlib.sha1(f"{asset}{time.time()}".encode()).hexdigest(),
                                "best_bid": best_bid,
                                "best_ask": best_ask,
                            }
                        ],
                        "timestamp": str(int(time.time() * 1000)),
                    }
                try:
                    await ws.send(json.dumps(frame))
                except ConnectionClosed:
                    return
                self.frames_sent += 1


class SportsChannel:
    def __init__(self, games: int, rate: float, seed: int) -> None:
        self.games = games
        self.rate = rate
        self.rng = random.Random(seed)

    async def handler(self, ws: ServerConnection) -> None:
        async def pinger() -> None:
            while True:
                await asyncio.sleep(5)
                await ws.send("ping")

        async def reader() -> None:
            async for _ in ws:  # the client's "pong" replies; unread they would stall the socket
                pass

        ping_task = asyncio.create_task(pinger())
        read_task = asyncio.create_task(reader())
        try:
            while True:
                await asyncio.sleep(1 / self.rate)
                game = self.rng.randrange(self.games)
                frame = {
                    "gameId": 5_000_000 + game,
                    "leagueAbbreviation": "atp",
                    "homeTeam": f"Home {game}",
                    "awayTeam": f"Away {game}",
                    "status": "InProgress",
                    "live": True,
                    "ended": False,
                    "score": f"6-{self.rng.randint(0, 5)}, {self.rng.randint(0, 6)}-{self.rng.randint(0, 6)}",
                    "period": "S2",
                    "elapsed": f"{self.rng.randint(0, 120)}",
                }
                await ws.send(json.dumps(frame))
        except ConnectionClosed:
            pass
        finally:
            ping_task.cancel()
            read_task.cancel()


def make_http_handler(world: World) -> type[BaseHTTPRequestHandler]:
    rewards_pages = 20

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, body: str, status: int = 200) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("cf-ray", "8f0000000000abcd-FRA")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            path = url.path
            if path == "/api/geoblock":
                self._send(
                    json.dumps(
                        {"blocked": False, "ip": "127.0.0.1", "country": "AM", "region": "ER"}
                    )
                )
            elif path.startswith("/tags/slug/"):
                slug = path.rsplit("/", 1)[1]
                self._send(json.dumps({"id": str(TAG_IDS[slug]), "slug": slug}))
            elif path == "/events/keyset":
                tag_id = int(query["tag_id"][0])
                limit = int(query.get("limit", ["100"])[0])
                cursor = query.get("after_cursor", [""])[0]
                offset = int(cursor.rsplit("-", 1)[1]) if cursor else 0
                self._send(world.keyset_page(tag_id, offset, limit))
            elif path == "/time":
                self._send(str(int(time.time())))
            elif path.startswith("/clob-markets/"):
                cid = path.rsplit("/", 1)[1]
                self._send(
                    json.dumps({"condition_id": cid, "minimum_tick_size": 0.01, "fee_rate_bps": 0})
                )
            elif path == "/rewards/markets/current":
                page = int(query.get("next_cursor", ["0"])[0] or 0)
                data = [
                    {
                        "condition_id": hex64("reward", page, k),
                        "rewards_max_spread": 3.5,
                        "rewards_min_size": 50,
                        "total_daily_rate": 5,
                        "rewards_config": [{"rate_per_day": 5, "start_date": 1, "end_date": None}],
                    }
                    for k in range(100)
                ]
                nxt = str(page + 1) if page + 1 < rewards_pages else "LTE="
                self._send(json.dumps({"data": data, "next_cursor": nxt}))
            elif path in ("/sports", "/sports/market-types"):
                self._send("[]")
            else:
                self._send(json.dumps({"error": "not found"}), 404)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"[]")
            if urlparse(self.path).path == "/books":
                books = [
                    {
                        "market": world.cid_of.get(str(item.get("token_id")), ""),
                        "asset_id": str(item.get("token_id")),
                        "bids": [{"price": "0.01", "size": "10"}],
                        "asks": [{"price": "0.99", "size": "10"}],
                        "hash": "rest-hash-differs",
                        "timestamp": str(int(time.time() * 1000)),
                    }
                    for item in body
                    if isinstance(item, dict)
                ]
                self._send(json.dumps(books))
            else:
                self._send(json.dumps({"error": "not found"}), 404)

    return Handler


async def serve_ws(world: World, ws_port: int, sports_port: int, seed: int) -> None:
    market = MarketChannel(world, seed)
    sports = SportsChannel(games=200, rate=10.0, seed=seed + 1)
    async with (
        serve(market.handler, "127.0.0.1", ws_port, max_size=None),
        serve(sports.handler, "127.0.0.1", sports_port),
    ):
        await asyncio.Future()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--http-port", type=int, required=True)
    p.add_argument("--ws-port", type=int, required=True)
    p.add_argument("--sports-port", type=int, required=True)
    p.add_argument("--matches", default="tennis=600,soccer=800,basketball=200")
    p.add_argument("--futures", default="tennis=20,soccer=60,basketball=20")
    p.add_argument("--side-markets", type=int, default=6)
    p.add_argument("--levels", type=int, default=40)
    p.add_argument("--rate", type=float, default=800.0)
    p.add_argument("--desc-bytes", type=int, default=1200)
    p.add_argument("--churn-every", type=float, default=30.0)
    p.add_argument("--churn-step", type=int, default=20)
    p.add_argument("--seed", type=int, default=7)
    return p.parse_args()


def _counts(spec: str) -> dict[str, int]:
    return {k: int(v) for k, v in (item.split("=") for item in spec.split(","))}


def main() -> None:
    args = parse_args()
    scale = Scale(
        matches=_counts(args.matches),
        futures=_counts(args.futures),
        side_markets=args.side_markets,
        levels=args.levels,
        rate=args.rate,
        desc_bytes=args.desc_bytes,
        churn_every_s=args.churn_every,
        churn_step=args.churn_step,
    )
    world = World(scale, args.seed)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.http_port), make_http_handler(world))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(
        json.dumps({"ready": True, "events": {s: len(e) for s, e in world.events.items()}}),
        flush=True,
    )
    asyncio.run(serve_ws(world, args.ws_port, args.sports_port, args.seed))


if __name__ == "__main__":
    main()
