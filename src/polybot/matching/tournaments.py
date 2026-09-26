"""Tennis tour level from tournament/category names (heuristic, used for coverage stats).

Polymarket slugs use `atp-` for Challenger and ITF matches too (docs/api_notes.md §11), so
the level comes from the odds provider's tournament and category names. Unknown names
fall into "other" and are listed in the report instead of being guessed.
"""

from __future__ import annotations

from polybot.matching.names import fold, team_similarity

ATP_WTA = "atp_wta"
CHALLENGER = "challenger"
ITF = "itf"
OTHER = "other"


def tennis_level(*names: str) -> str:
    text = " ".join(fold(n) for n in names if n)
    tokens = set(text.split())
    if "itf" in tokens or "futures" in tokens:
        return ITF
    if "challenger" in tokens or ("wta" in tokens and "125" in tokens):
        return CHALLENGER
    if tokens & {"atp", "wta"} or any(
        slam in text for slam in ("australian open", "roland garros", "wimbledon", "us open")
    ):
        return ATP_WTA
    return OTHER


def title_tournament(title: str) -> str:
    """Polymarket match titles look like "Cincinnati Open: Jiri Lehecka vs Arthur Fils"."""
    head, sep, _ = title.partition(": ")
    return head.replace("(Doubles)", "").strip() if sep else ""


def best_tournament(name: str, candidates: dict[int, str], min_score: float = 0.85) -> int | None:
    """Tournament id whose name best matches `name`, if the match is clear."""
    if not name:
        return None
    scored = sorted(
        ((team_similarity(name, cand), tid) for tid, cand in candidates.items()), reverse=True
    )
    if not scored or scored[0][0] < min_score:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05:
        return None
    return scored[0][1]
