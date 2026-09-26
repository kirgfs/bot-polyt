"""Leagues of the mini-bot by their Gamma `/sports` codes (docs/api_notes.md §11).

A `/sports` entry per league carries its code (`sea` Serie A, `lal` La Liga, `fl1` Ligue 1,
`ere` Eredivisie), tag ids and a series id (the league season) [CAP]. Matches are listed by
the series and by the league's own tags together: the series found every match the tags
did, and more in some leagues; the league-name tags (`serie-a`, `la-liga`) mostly carry
season-long markets. The shared tags (`sports`, `games`, `soccer`) are never league tags:
listing them would page through every game of every sport. They are excluded twice: by id
(resolved from their slugs at start) and by being in more than two `/sports` entries.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

# Shared tags, resolved to ids at start (docs/api_notes.md §11 [CAP]: on every soccer match).
GENERIC_TAG_SLUGS = ("sports", "games", "soccer")
# A tag in more `/sports` entries than this is shared too, not a league's own.
MAX_ENTRIES_PER_LEAGUE_TAG = 2
_IDS = re.compile(r"\d+")


@dataclass(frozen=True, slots=True)
class League:
    code: str
    tag_ids: tuple[int, ...]
    series_ids: tuple[int, ...]


def _ids(value: object) -> list[int]:
    """`/sports` lists ids in a string, e.g. "1,780,100639,100350" [CAP]."""
    return [int(x) for x in _IDS.findall(str(value or ""))]


def _entries(sports: object) -> list[dict[str, object]]:
    return [e for e in sports if isinstance(e, dict)] if isinstance(sports, list) else []


def _league(entry: dict[str, object], shared: Counter[int], generic: frozenset[int]) -> League:
    tags = tuple(
        t
        for t in _ids(entry.get("tags"))
        if t not in generic and shared[t] <= MAX_ENTRIES_PER_LEAGUE_TAG
    )
    return League(str(entry.get("sport") or ""), tags, tuple(_ids(entry.get("series"))))


def resolve_leagues(
    sports: object, codes: Iterable[str], generic: Iterable[int]
) -> tuple[dict[str, League], list[str]]:
    """(leagues found, codes missing or without any listing) from a `/sports` response."""
    entries = _entries(sports)
    shared = Counter(tag for e in entries for tag in set(_ids(e.get("tags"))))
    excluded = frozenset(generic)
    wanted = list(dict.fromkeys(codes))
    found: dict[str, League] = {}
    for entry in entries:
        league = _league(entry, shared, excluded)
        if (
            league.code in wanted
            and league.code not in found
            and (league.tag_ids or league.series_ids)
        ):
            found[league.code] = league
    return found, [c for c in wanted if c not in found]


def sport_codes(sports: object, sport_tag: int, generic: Iterable[int]) -> list[League]:
    """All `/sports` entries of one sport (by its tag, e.g. soccer): codes to choose from."""
    entries = _entries(sports)
    shared = Counter(tag for e in entries for tag in set(_ids(e.get("tags"))))
    excluded = frozenset(generic)
    leagues = [_league(e, shared, excluded) for e in entries if sport_tag in _ids(e.get("tags"))]
    return sorted(leagues, key=lambda league: league.code)


def league_of_slug(slug: str, codes: Iterable[str]) -> str:
    """League code of a match event from its slug, e.g. "lal-mala-esp-2026-10-09" → "lal" [CAP]."""
    prefix = slug.split("-", 1)[0]
    return prefix if prefix in set(codes) else ""
