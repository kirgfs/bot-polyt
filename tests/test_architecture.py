"""Guards for architecture rules that code review alone tends to miss."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "polybot"
# Venue-neutral layers (docs/architecture.md §12): may import venues.base only.
NEUTRAL_PACKAGES = ("strategy", "pricing", "risk")


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_strategy_layers_do_not_import_venue_adapters() -> None:
    offenders = []
    for package in NEUTRAL_PACKAGES:
        for path in (SRC / package).rglob("*.py") if (SRC / package).exists() else []:
            for module in imported_modules(path):
                if module.startswith("polybot.venues.") and module != "polybot.venues.base":
                    offenders.append(f"{path.relative_to(SRC)} imports {module}")
    assert not offenders, offenders


def test_recorder_is_read_only() -> None:
    """M1 recorder code paths never reach order endpoints (CLAUDE.md, rule 1)."""
    forbidden = ('"/order', '"/cancel', "post_order", "place_order", "polymarket.clients")
    paths = [*(SRC / "recorder").rglob("*.py"), *(SRC / "venues" / "polymarket").rglob("*.py")]
    offenders = [
        f"{path.name}: {token}"
        for path in paths
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders
