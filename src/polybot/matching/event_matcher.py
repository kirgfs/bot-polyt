"""Polymarket event ↔ odds-provider fixture matching, v0 (M1; full version with rules in M2).

Order of evidence:
1. exact: Gamma `sportsradarMatchId` equals the fixture's Betradar id (docs/api_notes.md §11);
2. fuzzy: same sport, start times within a window, both participants similar (either
   orientation). Accepted only with a clear margin over the runner-up.

Refusing is always allowed; a false match is the one outcome we must not produce.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from polybot.core.config import SportName
from polybot.core.timeutil import NS_PER_S
from polybot.feeds.odds.oddspapi_models import OpFixture, digits_id
from polybot.matching.names import person_similarity, team_similarity
from polybot.venues.polymarket.markets import PmEvent

_VS = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)
_YES_NO = frozenset({"yes", "no"})


class Method(StrEnum):
    BETRADAR_ID = "betradar_id"
    FUZZY = "fuzzy"
    AMBIGUOUS = "ambiguous"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class MatcherConfig:
    accept_score: float = 0.85
    min_margin: float = 0.10
    # Tennis start times are estimates for "followed by" matches; team sports are fixed.
    window_s: dict[str, float] | None = None

    def window_ns(self, sport: SportName) -> int:
        default = {"tennis": 12 * 3600.0, "soccer": 2 * 3600.0, "basketball": 2 * 3600.0}
        window = (self.window_s or default).get(sport, default[sport])
        return int(window * NS_PER_S)


@dataclass(frozen=True, slots=True)
class MatchResult:
    event_id: str
    fixture_id: str | None
    method: Method
    score: float
    runner_up: float
    swapped: bool | None  # True: fixture participant1 is our second participant
    reason: str


def participants(event: PmEvent) -> tuple[str, str] | None:
    """The two sides of a game, in Polymarket's order."""
    if event.home_name and event.away_name:
        return event.home_name, event.away_name
    for market in event.markets:
        if (
            market.sports_market_type == "moneyline"
            and len(market.outcomes) == 2
            and not {o.lower() for o in market.outcomes} & _YES_NO
        ):
            return market.outcomes[0], market.outcomes[1]
    title = event.title.rsplit(": ", 1)[-1]
    parts = _VS.split(title)
    if len(parts) == 2 and all(p.strip() for p in parts):
        return parts[0].strip(), parts[1].strip()
    return None


def _similarity(sport: SportName) -> Callable[[str, str], float]:
    return person_similarity if sport == "tennis" else team_similarity


def _pair_score(sport: SportName, sides: tuple[str, str], fixture: OpFixture) -> tuple[float, bool]:
    sim = _similarity(sport)
    direct = min(sim(sides[0], fixture.p1_name), sim(sides[1], fixture.p2_name))
    swapped = min(sim(sides[0], fixture.p2_name), sim(sides[1], fixture.p1_name))
    return (swapped, True) if swapped > direct else (direct, False)


def match_event(
    event: PmEvent,
    fixtures: Iterable[OpFixture],
    *,
    sport_id: int | None,
    cfg: MatcherConfig,
    use_ids: bool = True,
) -> MatchResult:
    sides = participants(event)
    start = event.game_start_ns
    same_sport = [f for f in fixtures if sport_id is None or f.sport_id in (None, sport_id)]

    pm_betradar = digits_id(event.sportsradar_match_id)
    if use_ids and pm_betradar is not None:
        by_id = [f for f in same_sport if f.betradar_id == pm_betradar]
        if len(by_id) == 1:
            fixture = by_id[0]
            score, swapped = _pair_score(event.sport, sides, fixture) if sides else (1.0, None)
            return MatchResult(
                event.event_id,
                fixture.fixture_id,
                Method.BETRADAR_ID,
                1.0,
                0.0,
                swapped,
                f"betradar {pm_betradar}; name score {score:.2f}",
            )

    if sides is None:
        return MatchResult(event.event_id, None, Method.NONE, 0.0, 0.0, None, "no participants")
    if start is None:
        return MatchResult(event.event_id, None, Method.NONE, 0.0, 0.0, None, "no start time")
    window = cfg.window_ns(event.sport)
    scored = sorted(
        (
            (*_pair_score(event.sport, sides, f), f)
            for f in same_sport
            if f.start_ns is not None and abs(f.start_ns - start) <= window
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    if not scored:
        return MatchResult(
            event.event_id, None, Method.NONE, 0.0, 0.0, None, "no fixture in window"
        )
    best_score, best_swapped, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < cfg.accept_score:
        return MatchResult(
            event.event_id,
            None,
            Method.NONE,
            best_score,
            runner_up,
            None,
            f"best {best.fixture_id} below threshold",
        )
    if best_score - runner_up < cfg.min_margin:
        return MatchResult(
            event.event_id,
            None,
            Method.AMBIGUOUS,
            best_score,
            runner_up,
            None,
            f"{best.fixture_id} vs {scored[1][2].fixture_id} too close",
        )
    return MatchResult(
        event.event_id, best.fixture_id, Method.FUZZY, best_score, runner_up, best_swapped, "ok"
    )
