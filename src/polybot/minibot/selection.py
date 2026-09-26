"""Which soccer markets the mini-bot quotes (docs/architecture.md §13).

Input: moneyline markets discovery found for the configured league tags. A market is a
candidate when it is a Yes/No market with a known start far enough away, accepting orders,
with rules we may quote (reviewed, or unreviewed in paper mode when allowed). Markets with
a rewards program come first, then the nearest kickoffs.

Markets already being quoted keep their place: they stay until the pull before kickoff
(no minimum quoting window applies to them) and rank ahead of new candidates, so a
refresh does not churn the book.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from polybot.core.config import MiniBotConfig
from polybot.core.timeutil import NS_PER_S
from polybot.minibot.rules import rules_template, team_names
from polybot.strategy.stage0 import RewardParams
from polybot.venues.polymarket.markets import PmEvent, PmMarket

NS_PER_MIN = 60 * NS_PER_S


@dataclass(frozen=True, slots=True)
class Candidate:
    event: PmEvent
    market: PmMarket
    token: str  # Yes token: the one we quote
    yes_index: int
    start_ns: int
    rewards: RewardParams | None
    template_id: str
    reviewed: bool
    league: str

    @property
    def title(self) -> str:
        return self.event.title

    @property
    def label(self) -> str:
        return self.market.question or self.market.slug


@dataclass(frozen=True, slots=True)
class Template:
    text: str  # normalized: team names, dates and numbers replaced
    example_title: str
    example_description: str


@dataclass
class SelectionResult:
    chosen: list[Candidate] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    templates: dict[str, Template] = field(default_factory=dict)
    eligible: int = 0


def rewards_params(market: PmMarket, unit: str) -> RewardParams | None:
    v, size, rate = market.rewards_max_spread, market.rewards_min_size, market.rewards_daily_rate
    if v is None or rate is None or v <= 0 or rate <= 0:
        return None
    return RewardParams(v / 100 if unit == "cents" else v, size or 0.0, rate)


def yes_index(market: PmMarket) -> int | None:
    labels = [o.strip().lower() for o in market.outcomes]
    return labels.index("yes") if labels.count("yes") == 1 and len(labels) == 2 else None


def league_of(event: PmEvent, leagues: Iterable[str]) -> str:
    return next((slug for slug in leagues if slug in event.tag_slugs), "")


def select(
    pairs: Iterable[tuple[PmEvent, PmMarket]],
    cfg: MiniBotConfig,
    now_ns: int,
    *,
    paper: bool,
    active: frozenset[str] = frozenset(),
) -> SelectionResult:
    result = SelectionResult()
    q, sel, rules = cfg.quoting, cfg.selection, cfg.rules
    pull_at = now_ns + int(q.pull_before_start_min * NS_PER_MIN)
    earliest_new = pull_at + int(sel.min_quote_window_min * NS_PER_MIN)
    latest = now_ns + int(sel.horizon_h * 60 * NS_PER_MIN)
    candidates: list[Candidate] = []
    for event, market in pairs:
        index = yes_index(market)
        start = market.game_start_ns
        if index is None or len(market.token_ids) != 2:
            result.skipped["not_yes_no"] += 1
            continue
        token = market.token_ids[index]
        earliest = pull_at if token in active else earliest_new
        if start is None or start < earliest:
            result.skipped["too_close_to_start"] += 1
            continue
        if start > latest:
            result.skipped["too_far"] += 1
            continue
        if market.accepting_orders is False or market.closed is True:
            result.skipped["not_accepting_orders"] += 1
            continue
        if not market.description:
            result.skipped["no_rules_text"] += 1
            continue
        names = team_names(event.title, event.home_name, event.away_name)
        template_id, text = rules_template(market.description, names)
        if template_id not in result.templates:
            result.templates[template_id] = Template(text, event.title, market.description)
        reviewed = template_id in rules.approved_templates
        if not reviewed and not (paper and rules.paper_quote_unreviewed):
            result.skipped["rules_not_reviewed"] += 1
            continue
        reward = rewards_params(market, q.rewards_spread_unit)
        if q.require_rewards and reward is None:
            result.skipped["no_rewards"] += 1
            continue
        candidates.append(
            Candidate(
                event=event,
                market=market,
                token=token,
                yes_index=index,
                start_ns=start,
                rewards=reward,
                template_id=template_id,
                reviewed=reviewed,
                league=league_of(event, cfg.leagues),
            )
        )
    result.eligible = len(candidates)
    candidates.sort(
        key=lambda c: (
            c.token not in active,
            -(c.rewards.daily_rate if c.rewards else 0.0),
            c.start_ns,
            c.token,
        )
    )
    result.chosen = candidates[: sel.max_markets]
    result.skipped["over_max_markets"] += len(candidates) - len(result.chosen)
    return result
