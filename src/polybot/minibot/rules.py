"""Resolution-rule templates (CLAUDE.md, rule 4).

Soccer descriptions differ only in team names, dates and times. Replacing those gives a
template; its hash is what a human approves in `config/minibot.yaml` after reading the
text. Real orders will require an approved template; paper mode may quote unreviewed ones
when the config allows, and marks them in every report.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

_MONTHS = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b"
)
_WEEKDAYS = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")
_NUMBERS = re.compile(r"\d+")
_SPACES = re.compile(r"\s+")
_VS = re.compile(r"\s+(?:vs\.?|v\.?|-)\s+", re.IGNORECASE)


def team_names(title: str, *names: str | None) -> list[str]:
    """Names to blank out: explicit team names plus both sides of a "A vs. B" title."""
    found = [n for n in names if n]
    found += [part.strip() for part in _VS.split(title) if part.strip()]
    return found


def rules_template(description: str, names: Iterable[str]) -> tuple[str, str]:
    """(template id, normalized text) of a market description."""
    text = description.lower()
    for name in sorted({n.lower() for n in names if len(n) > 2}, key=len, reverse=True):
        text = text.replace(name, "<team>")
    text = _MONTHS.sub("<month>", text)
    text = _WEEKDAYS.sub("<weekday>", text)
    text = _NUMBERS.sub("#", text)
    text = _SPACES.sub(" ", text).strip()
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:12], text
