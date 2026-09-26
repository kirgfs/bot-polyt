from __future__ import annotations

import pytest

from polybot.core.timeutil import NS_PER_S, parse_ts_ns
from polybot.feeds.odds.oddspapi_models import OpFixture
from polybot.matching.event_matcher import MatcherConfig, Method, match_event, participants
from polybot.matching.names import fold, person_similarity, team_similarity
from polybot.matching.tournaments import (
    ATP_WTA,
    CHALLENGER,
    ITF,
    OTHER,
    tennis_level,
    title_tournament,
)
from polybot.venues.polymarket.markets import ParseIssues, PmEvent, parse_event
from tests.conftest import load_json

START = parse_ts_ns("2026-08-18T01:15:00Z") or 0


def fixture(
    fid: str, p1: str, p2: str, *, start: int = START, betradar: str | None = None
) -> OpFixture:
    return OpFixture(fid, 12, 1, "ATP Cincinnati", "ATP", p1, p2, start, 0, True, betradar)


def events() -> dict[str, PmEvent]:
    raw = load_json("gamma_events_tennis.json")["events"]
    return {e["slug"]: parse_event(e, "tennis", ParseIssues()) for e in raw}


class TestNames:
    def test_fold(self) -> None:
        assert fold("Jiří Lehečka") == "jiri lehecka"
        assert fold("Casper Ruud-Ødegaard") == "casper ruud odegaard"

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Jiri Lehecka", "Jiri Lehecka"),
            ("Jiri Lehecka", "Lehecka, Jiri"),
            ("Jiri Lehecka", "Jiří Lehečka"),
            ("Felix Auger-Aliassime", "Auger-Aliassime, Felix"),
        ],
    )
    def test_same_person(self, a: str, b: str) -> None:
        assert person_similarity(a, b) == 1.0

    @pytest.mark.parametrize(
        ("a", "b"), [("Jiri Lehecka", "Lehecka J."), ("Jiri Lehecka", "Lehecka")]
    )
    def test_subset_names(self, a: str, b: str) -> None:
        assert person_similarity(a, b) >= 0.9

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Elmer Moeller", "Marvin Moeller"),
            ("Elmer Moeller", "Moeller M."),
            ("Jiri Lehecka", "Arthur Fils"),
        ],
    )
    def test_different_people(self, a: str, b: str) -> None:
        assert person_similarity(a, b) < 0.7

    def test_teams(self) -> None:
        assert team_similarity("Manchester United FC", "Manchester United") == 1.0
        assert team_similarity("Real Madrid", "Atletico Madrid") < 0.85


class TestMatcher:
    def test_participants_from_moneyline_outcomes(self) -> None:
        assert participants(events()["atp-lehecka-fils-2026-08-17"]) == (
            "Jiri Lehecka",
            "Arthur Fils",
        )

    def test_betradar_id_is_exact(self) -> None:
        event = events()["atp-lehecka-fils-2026-08-17"]
        pool = [
            fixture("f1", "Somebody", "Else", betradar="61098461"),
            fixture("f2", "Lehecka J.", "Fils A."),
        ]
        result = match_event(event, pool, sport_id=12, cfg=MatcherConfig())
        assert (result.fixture_id, result.method) == ("f1", Method.BETRADAR_ID)

    def test_fuzzy_with_reversed_order(self) -> None:
        event = events()["atp-lehecka-fils-2026-08-17"]
        pool = [fixture("f1", "Fils, Arthur", "Lehecka, Jiri"), fixture("f2", "Other A", "Other B")]
        result = match_event(event, pool, sport_id=12, cfg=MatcherConfig(), use_ids=False)
        assert (result.fixture_id, result.method, result.swapped) == ("f1", Method.FUZZY, True)

    def test_shared_surname_is_not_matched(self) -> None:
        event = events()["atp-moeller-ivanov-2026-08-17"]
        start = event.game_start_ns or 0
        pool = [fixture("f1", "Marvin Moeller", "Ivan Ivanov", start=start)]
        result = match_event(event, pool, sport_id=12, cfg=MatcherConfig())
        assert result.fixture_id is None

    def test_two_close_candidates_are_ambiguous(self) -> None:
        event = events()["atp-lehecka-fils-2026-08-17"]
        pool = [fixture("f1", "Lehecka J.", "Fils A."), fixture("f2", "Lehecka", "Fils")]
        result = match_event(event, pool, sport_id=12, cfg=MatcherConfig(), use_ids=False)
        assert result.method is Method.AMBIGUOUS and result.fixture_id is None

    def test_outside_time_window(self) -> None:
        event = events()["atp-lehecka-fils-2026-08-17"]
        pool = [fixture("f1", "Jiri Lehecka", "Arthur Fils", start=START + 13 * 3600 * NS_PER_S)]
        result = match_event(event, pool, sport_id=12, cfg=MatcherConfig(), use_ids=False)
        assert result.fixture_id is None and result.reason == "no fixture in window"


class TestTournaments:
    @pytest.mark.parametrize(
        ("names", "level"),
        [
            (("Cincinnati", "ATP"), ATP_WTA),
            (("Challenger Sion", "ATP"), CHALLENGER),
            (("M25 Roehampton", "ITF Men"), ITF),
            (("WTA 125 Florence",), CHALLENGER),
            (("Wimbledon",), ATP_WTA),
            (("UTR Pro Series",), OTHER),
        ],
    )
    def test_levels(self, names: tuple[str, ...], level: str) -> None:
        assert tennis_level(*names) == level

    def test_title_tournament(self) -> None:
        assert (
            title_tournament("Cincinnati Open (Doubles): Cash/Glasspool vs Ram/Salisbury")
            == "Cincinnati Open"
        )
        assert title_tournament("No prefix here") == ""
