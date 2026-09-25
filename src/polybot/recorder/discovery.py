"""Gamma discovery loop: which sports events exist, which books to record.

Each poll lists open events per sport tag, parses them, stores changed events to Parquet
(structural changes immediately, a full snapshot every hour for volumes and prices) and
returns the set of markets whose books the recorder must subscribe to.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from polybot.core.config import RecorderConfig, SportName
from polybot.core.logging import get_logger
from polybot.core.timeutil import NS_PER_S, now_ns
from polybot.data.records import Kind, Record, RecordWriter, Source
from polybot.venues.polymarket.gamma import GammaClient
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

    async def _list_all(
        self, issues: ParseIssues
    ) -> tuple[dict[str, tuple[PmEvent, dict[str, Any]]], list[str]]:
        disc = self._cfg.discovery
        found: dict[str, tuple[PmEvent, dict[str, Any]]] = {}
        truncated: list[str] = []
        for sport, tag_ids in self.tag_ids.items():
            for tag_id in tag_ids:
                page = await self._gamma.list_events(
                    tag_id=tag_id,
                    page_size=disc.page_size,
                    max_pages=disc.max_pages,
                    require_tag_ids=self.required_tag_ids.get(sport, ()),
                )
                if page.truncated:
                    truncated.append(f"{sport}:{tag_id}")
                    log.warning("gamma_listing_truncated", sport=sport, tag_id=tag_id)
                for raw in page.events:
                    event_id = str(raw.get("id", ""))
                    if event_id and event_id not in found:
                        found[event_id] = (parse_event(raw, sport, issues), raw)
        return found, truncated

    def _store(
        self, found: dict[str, tuple[PmEvent, dict[str, Any]]], now: int, full_due: bool
    ) -> tuple[dict[str, PmEvent], int, int]:
        """Write changed (or all, on a full snapshot) tracked events; mark vanished ones."""
        disc = self._cfg.discovery
        lookback = int(disc.live_lookback_h * NS_PER_H)
        track_until = now + int(disc.track_horizon_h * NS_PER_H)
        written = 0
        tracked: dict[str, PmEvent] = {}
        for event_id, (event, raw) in found.items():
            start = event.game_start_ns
            if not event.is_match or start is None or not (now - lookback <= start <= track_until):
                continue
            tracked[event_id] = event
            fingerprint = structural_fingerprint(raw)
            if full_due or self._fingerprints.get(event_id) != fingerprint:
                self._sink.write(
                    Record(
                        ts_recv_ns=now,
                        source=Source.GAMMA_EVENTS,
                        kind=Kind.REST,
                        event_type=event.sport,
                        key=event_id,
                        endpoint="/events/keyset",
                        payload=json.dumps(raw, separators=(",", ":")),
                    )
                )
                written += 1
            self._fingerprints[event_id] = fingerprint
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
        return tracked, written, len(gone)

    async def poll(self) -> DiscoveryResult:
        disc = self._cfg.discovery
        now = now_ns()
        full_due = now - self._last_full_ns >= disc.full_snapshot_interval_s * NS_PER_S
        issues = ParseIssues()
        found, truncated = await self._list_all(issues)
        tracked, written, gone = self._store(found, now, full_due)
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
        counts: dict[str, int] = {}
        for event in tracked.values():
            counts[f"events_{event.sport}"] = counts.get(f"events_{event.sport}", 0) + 1
        for event, _market in selected:
            counts[f"books_{event.sport}"] = counts.get(f"books_{event.sport}", 0) + 1
        self.stats.polls += 1
        self.stats.last_poll_ns = now
        self.stats.last_counts = counts
        self.stats.last_issues = issues.as_dict()
        summary = {
            "counts": counts,
            "issues": issues.as_dict(),
            "full_snapshot": full_due,
            "written": written,
            "events_listed": len(found),
            "gone": gone,
            "truncated": truncated,
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
