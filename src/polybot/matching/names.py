"""Name normalization and similarity for players and teams.

Players: order-insensitive ("Lehecka, Jiri" = "Jiri Lehecka"), diacritics-insensitive
("Lehečka" = "Lehecka"), initials-aware ("Lehecka J." = "Jiri Lehecka"), and strict about
conflicting given names ("Elmer Moeller" != "Marvin Moeller"): a wrong match is worse
than no match (CLAUDE.md, fail-closed).
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
# Letters without a Unicode decomposition (ø, ł, đ, ß, æ) need explicit mapping.
_EXTRA_FOLD = str.maketrans(
    {"ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ß": "ss", "æ": "ae", "Æ": "AE"}
)

# Tokens that carry no identity in team names.
_TEAM_STOPWORDS = frozenset(
    [
        "fc",
        "cf",
        "sc",
        "afc",
        "ac",
        "as",
        "club",
        "de",
        "del",
        "la",
        "the",
        "cd",
        "sd",
        "ud",
        "fk",
        "sk",
        "bc",
        "bk",
        "basketball",
        "basket",
        "calcio",
        "sv",
        "vfl",
        "vfb",
    ]
)


def fold(text: str) -> str:
    """Lowercase ASCII without diacritics and punctuation, single spaces."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_ALNUM.sub(" ", stripped.translate(_EXTRA_FOLD).lower()).strip()


def person_tokens(name: str) -> list[str]:
    """Tokens of a person name; "Surname, Given" is reordered to "Given Surname"."""
    if "," in name:
        surname, _, given = name.partition(",")
        name = f"{given} {surname}"
    return fold(name).split()


def person_similarity(a: str, b: str) -> float:
    """1.0 same person, ~0.95 one name is a subset of the other, <= 0.6 on conflicts."""
    ta, tb = person_tokens(a), person_tokens(b)
    if not ta or not tb:
        return 0.0
    full_a = {t for t in ta if len(t) > 1}
    full_b = {t for t in tb if len(t) > 1}
    initials_a = {t for t in ta if len(t) == 1}
    initials_b = {t for t in tb if len(t) == 1}
    common = full_a & full_b
    if not common:
        # Transliteration variants ("Aleksandr Bublik" / "Alexander Bublik") only when the
        # rest is nearly identical; otherwise treat as different people.
        ratio = fuzz.token_sort_ratio(" ".join(ta), " ".join(tb)) / 100.0
        return 0.8 * ratio if ratio >= 0.9 else 0.3 * ratio
    rest_a, rest_b = full_a - common, full_b - common
    if not rest_a and not rest_b:
        return 1.0 if not (initials_a and initials_b and initials_a != initials_b) else 0.3
    if rest_a and rest_b:
        # Both have extra given names: same person only if they are spelling variants.
        variant = fuzz.ratio(" ".join(sorted(rest_a)), " ".join(sorted(rest_b))) / 100.0
        return 0.6 if variant >= 0.8 else 0.25
    # One side is a subset (e.g. surname only, or surname + initial).
    extra, initials = (rest_a, initials_b) if rest_a else (rest_b, initials_a)
    if initials and not initials <= {t[0] for t in extra}:
        return 0.25
    return 0.95


def team_similarity(a: str, b: str) -> float:
    ta = [t for t in fold(a).split() if t not in _TEAM_STOPWORDS]
    tb = [t for t in fold(b).split() if t not in _TEAM_STOPWORDS]
    if not ta or not tb:
        return 0.0
    return fuzz.token_set_ratio(" ".join(ta), " ".join(tb)) / 100.0
