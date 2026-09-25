"""Gamma discovery loop: which sports events exist, which books to record.

Each poll lists open events per sport tag, parses them, stores changed events to Parquet
(structural changes immediately; all tracked events on a periodic full snapshot and at
the first poll of each UTC day) and returns the markets whose books to subscribe to.

Memory: listings are processed page by page and raw events are dropped as soon as they
are written; only slim parsed events (markets of the recorded types) are kept between
polls. A full listing of a large sport is tens of MB of JSON and several times that as
Python objects, so holding it whole is what pushed the recorder into the OOM killer.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from polybot.core.config import RecorderConfig, SportName
from polybot.core.logging import get_logger
from polybot.core.timeutil import NS_PER_S, now_ns, ns_to_date
from polybot.data.records import Kind, Record, RecordWriter, Source, drain
from polybot.venues.polymarket.gamma import EventsPage, GammaClient
from polybot.venues.polymarket.markets import (
    ParseIssues,
    PmEvent,
    PmMarket,
    parse_event,
    recordable_markets,
)

log = get_logger(__name__)

NS_PER_H = 3600 * NS_PER_S

# Fields that change with every trade or quote. Excluded from the change fingerprint
# so that only structural changes (start time, status, rules, tokens, fees) trigger a
# write between hourly full snapshots. Unknown volatile fields only cost extra rows.
VOLATILE_KEYS = frozenset(
    {
        "volume",
        "volumeNum",
        "volume24hr",
        "volume1wk",
        "volume1mo",
        "volume1yr",
        "volumeClob",
        "volumeAmm",
        "volume24hrClob",
        "volume1wkClob",
        "volume1moClob",
        "volume1yrClob",
        "liquidity",
        "liquidityNum",
        "liquidityClob",
        "liquidityAmm",
        "outcomePrices",
        "bestBid",
        "bestAsk",
        "lastTradePrice",
        "spread",
        "oneHourPriceChange",
        "oneDayPriceChange",
        "oneWeekPriceChange",
        "oneMonthPriceChange",
        "oneYearPriceChange",
        "competitive",
        "openInterest",
        "commentCount",
        "updatedAt",
    }
)


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_volatile(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


def structural_fingerprint(raw_event: dict[str, Any]) -> str:
    canonical = json.dumps(_strip_volatile(raw_event), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()


def slim_event(event: PmEvent, market_types: frozenset[str]) -> PmEvent:
    """Keep only markets of the recorded types (side markets dominate the memory).

    An event without such markets keeps its earliest-starting market, so its start time
    stays known (OddsPapi matching, counts).
    """
    kept = tuple(m for m in event.markets if m.sports_market_type in market_types)
    if not kept:
        timed = [m for m in event.markets if m.game_start_ns is not None]
        kept = (min(timed, key=lambda m: m.game_start_ns or 0),) if timed else ()
    return dataclasses.replace(event, markets=kept)


def cap_markets(
    selected: list[tuple[PmEvent, PmMarket]], limit: int, now: int
) -> tuple[list[tuple[PmEvent, PmMarket]], int]:
    """At most `limit` markets, nearest to their start first; returns (kept, dropped)."""
    if len(selected) <= limit:
        return selected, 0
    ranked = sorted(
        selected,
        key=lambda em: (abs((em[1].game_start_ns or now) - now), em[1].condition_id),
    )
    return ranked[:limit], len(selected) - limit


@dataclass
class DiscoveryResult:
    events: dict[str, PmEvent]
    selected: list[tuple[PmEvent, PmMarket]]
    issues: ParseIssues
    written: int
    full_snapshot: bool

    @property
    def token_ids(self) -> set[str]:
        return {token for _, market in self.selected for token in market.token_ids}

    @property
    def condition_ids(self) -> set[str]:
        return {market.condition_id for _, market in self.selected}


@dataclass
class DiscoveryStats:
    polls: int = 0
    errors: int = 0
    last_poll_ns: int = 0
    last_counts: dict[str, int] = field(default_factory=dict)
    last_issues: dict[str, object] = field(default_factory=dict)


class Discovery:
    def __init__(self, gamma: GammaClient, cfg: RecorderConfig, sink: RecordWriter) -> None:
        self._gamma = gamma
        self._cfg = cfg
        self._sink = sink
        self.tag_ids: dict[SportName, list[int]] = {}
        self.required_tag_ids: dict[SportName, tuple[int, ...]] = {}
        self._fingerprints: dict[str, str] = {}
        self._last_full_ns = 0
        self._last_capped = 0
        self._last_cap_log_ns = 0
        self.stats = DiscoveryStats()
        self.last_result: DiscoveryResult | None = None

    async def resolve_tags(self) -> None:
        for sport, sport_cfg in self._cfg.sports.items():
            self.tag_ids[sport] = [
                await self._gamma.resolve_tag_id(slug) for slug in sport_cfg.tag_slugs
            ]
            self.required_tag_ids[sport] = tuple(
                [await self._gamma.resolve_tag_id(slug) for slug in sport_cfg.require_tag_slugs]
            )
        log.info(
            "gamma_tags_resolved",
            tags=dict(self.tag_ids),
            required=dict(self.required_tag_ids),
        )

    def _full_snapshot_due(self, now: int) -> bool:
        if not self._last_full_ns:
            return True
        interval_ns = self._cfg.discovery.full_snapshot_interval_s * NS_PER_S
        return now - self._last_full_ns >= interval_ns or ns_to_date(now) != ns_to_date(
            self._last_full_ns
        )

    def _in_window(self, event: PmEvent, now: int) -> bool:
        disc = self._cfg.discovery
        start = event.game_start_ns
        return (
            event.is_match
            and start is not None
            and now - int(disc.live_lookback_h * NS_PER_H)
            <= start
            <= now + int(disc.track_horizon_h * NS_PER_H)
        )

    def _store(self, event_id: str, sport: str, raw: dict[str, Any], now: int, full: bool) -> int:
        """Write a tracked event if it changed structurally (or on a full snapshot)."""
        fingerprint = structural_fingerprint(raw)
        changed = self._fingerprints.get(event_id) != fingerprint
        self._fingerprints[event_id] = fingerprint
        if not (full or changed):
            return 0
        self._sink.write(
            Record(
                ts_recv_ns=now,
                source=Source.GAMMA_EVENTS,
                kind=Kind.REST,
                event_type=sport,
                key=event_id,
                endpoint="/events/keyset",
                payload=json.dumps(raw, separators=(",", ":")),
            )
        )
        return 1

    def _mark_gone(self, tracked: dict[str, PmEvent], now: int) -> int:
        gone = [eid for eid in self._fingerprints if eid not in tracked]
        for event_id in gone:
            del self._fingerprints[event_id]
            self._sink.write(
                Record(
                    ts_recv_ns=now,
                    source=Source.GAMMA_EVENTS,
                    kind=Kind.CONTROL,
                    event_type="event_gone",
                    key=event_id,
                    payload="{}",
                )
            )
        return len(gone)

    async def _scan(
        self, now: int, full: bool, issues: ParseIssues
    ) -> tuple[dict[str, PmEvent], int, int, list[str]]:
        """Page through all sport tags; returns (tracked, written, listed, truncated)."""
        disc = self._cfg.discovery
        tracked: dict[str, PmEvent] = {}
        seen: set[str] = set()
        written = 0
        truncated: list[str] = []
        for sport, tag_ids in self.tag_ids.items():
            market_types = frozenset(self._cfg.sports[sport].market_types)
            for tag_id in tag_ids:
                listing = EventsPage()
                async for raw_events in self._gamma.iter_events(
                    tag_id=tag_id,
                    page_size=disc.page_size,
                    max_pages=disc.max_pages,
                    require_tag_ids=self.required_tag_ids.get(sport, ()),
                    listing=listing,
                ):
                    for raw in raw_events:
                        event_id = str(raw.get("id", ""))
                        if not event_id or event_id in seen:
                            continue
                        seen.add(event_id)
                        event = parse_event(raw, sport, issues)
                        if not self._in_window(event, now):
                            continue
                        written += self._store(event_id, sport, raw, now, full)
                        tracked[event_id] = slim_event(event, market_types)
                    del raw_events
                    await drain(self._sink)  # a full snapshot must not outrun the disk
                if listing.truncated:
                    truncated.append(f"{sport}:{tag_id}")
                    log.warning("gamma_listing_truncated", sport=sport, tag_id=tag_id)
        return tracked, written, len(seen), truncated

    async def poll(self) -> DiscoveryResult:
        disc = self._cfg.discovery
        now = now_ns()
        full_due = self._full_snapshot_due(now)
        issues = ParseIssues()
        tracked, written, listed, truncated = await self._scan(now, full_due, issues)
        gone = self._mark_gone(tracked, now)
        if full_due:
            self._last_full_ns = now
        selected = recordable_markets(
            tracked.values(),
            market_types={s: c.market_types for s, c in self._cfg.sports.items()},
            exclude_doubles={s: c.exclude_doubles for s, c in self._cfg.sports.items()},
            now_ns=now,
            horizon_ns=int(disc.subscribe_horizon_h * NS_PER_H),
            lookback_ns=int(disc.live_lookback_h * NS_PER_H),
        )
        selected, capped = cap_markets(selected, disc.max_subscribed_markets, now)
        # Log when capping starts or stops, and hourly while it lasts (not every poll).
        if bool(capped) != bool(self._last_capped) or (
            capped and now - self._last_cap_log_ns >= 3600 * NS_PER_S
        ):
            log.warning(
                "subscribed_markets_capped",
                kept=len(selected),
                dropped=capped,
                limit=disc.max_subscribed_markets,
            )
            self._last_cap_log_ns = now
        self._last_capped = capped
        counts: dict[str, int] = {}
        for event in tracked.values():
            counts[f"events_{event.sport}"] = counts.get(f"events_{event.sport}", 0) + 1
        for event, _market in selected:
            counts[f"books_{event.sport}"] = counts.get(f"books_{event.sport}", 0) + 1
        if capped:
            counts["books_capped"] = capped
        self.stats.polls += 1
        self.stats.last_poll_ns = now
        self.stats.last_counts = counts
        self.stats.last_issues = issues.as_dict()
        summary = {
            "counts": counts,
            "issues": issues.as_dict(),
            "full_snapshot": full_due,
            "written": written,
            "events_listed": listed,
            "gone": gone,
            "truncated": truncated,
            "capped": capped,
        }
        self._sink.write(
            Record(
                ts_recv_ns=now,
                source=Source.GAMMA_EVENTS,
                kind=Kind.CONTROL,
                event_type="poll_summary",
                payload=json.dumps(summary),
            )
        )
        if issues.bad_game_start:
            log.warning("gamma_unparseable_game_start", examples=issues.bad_game_start[:3])
        result = DiscoveryResult(tracked, selected, issues, written, full_due)
        self.last_result = result
        return result
