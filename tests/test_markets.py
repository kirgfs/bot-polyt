from __future__ import annotations

from decimal import Decimal

from polybot.core.timeutil import NS_PER_S, parse_ts_ns
from polybot.venues.polymarket.markets import (
    ParseIssues,
    parse_event,
    parse_string_sequence,
    recordable_markets,
)
from tests.conftest import load_json

H = 3600 * NS_PER_S


def parsed_events() -> tuple[list, ParseIssues]:  # type: ignore[type-arg]
    issues = ParseIssues()
    events = [
        parse_event(e, "tennis", issues) for e in load_json("gamma_events_tennis.json")["events"]
    ]
    return events, issues


def test_json_encoded_sequences() -> None:
    assert parse_string_sequence('["Jiri Lehecka", "Arthur Fils"]') == (
        "Jiri Lehecka",
        "Arthur Fils",
    )
    assert parse_string_sequence(["1", 2]) == ("1", "2")
    assert parse_string_sequence(None) == ()
    assert parse_string_sequence("not json") is None
    assert parse_string_sequence('{"a": 1}') is None


def test_parse_real_shaped_events() -> None:
    events, issues = parsed_events()
    by_slug = {e.slug: e for e in events}
    lehecka = by_slug["atp-lehecka-fils-2026-08-17"]
    assert lehecka.is_match and not lehecka.is_doubles
    assert lehecka.game_id == "90001"
    assert lehecka.sportsradar_match_id == "sr:match:61098461"
    moneyline = next(m for m in lehecka.markets if m.sports_market_type == "moneyline")
    assert moneyline.outcomes == ("Jiri Lehecka", "Arthur Fils")
    assert moneyline.game_start_ns == parse_ts_ns("2026-08-18T01:15:00Z")
    assert moneyline.tick_size == Decimal("0.01")
    assert moneyline.seconds_delay == 3
    assert moneyline.has_description
    assert by_slug["atp-doubles-cashgla-ramsali-2026-08-16"].is_doubles
    assert not by_slug["2026-mens-us-open-winner-tennis"].is_match  # futures
    assert issues.bad_game_start == []


def test_unparseable_start_is_reported_not_guessed() -> None:
    raw = load_json("gamma_events_tennis.json")["events"][1]
    raw["markets"][0]["gameStartTime"] = "2026-08-18 13:00"  # no timezone
    issues = ParseIssues()
    event = parse_event(raw, "tennis", issues)
    assert event.markets[0].game_start_ns is None
    assert issues.bad_game_start == ["2026-08-18 13:00"]
    assert not event.is_match


def test_recordable_selection() -> None:
    events, _ = parsed_events()
    start = parse_ts_ns("2026-08-18T01:15:00Z")
    assert start is not None
    selected = recordable_markets(
        events,
        market_types={"tennis": ("moneyline",)},
        exclude_doubles={"tennis": True},
        now_ns=start - 2 * H,
        horizon_ns=72 * H,
        lookback_ns=6 * H,
    )
    slugs = {m.slug for _, m in selected}
    # Only singles moneyline within the horizon; doubles and other types excluded.
    assert slugs == {"atp-lehecka-fils-2026-08-17", "atp-moeller-ivanov-2026-08-17"}


def test_recordable_respects_horizon_and_lookback() -> None:
    events, _ = parsed_events()
    start = parse_ts_ns("2026-08-18T01:15:00Z")
    assert start is not None
    kwargs = {
        "market_types": {"tennis": ("moneyline",)},
        "exclude_doubles": {"tennis": True},
        "horizon_ns": 1 * H,
        "lookback_ns": 1 * H,
    }
    too_early = recordable_markets(events, now_ns=start - 3 * H, **kwargs)  # type: ignore[arg-type]
    assert not too_early
    too_late = recordable_markets(events, now_ns=start + 13 * H, **kwargs)  # type: ignore[arg-type]
    assert not too_late
