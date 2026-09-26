"""What does Gamma actually list for our sports? Output for configuring recorder.yaml.

Answers checklist item 15 (docs/api_notes.md §16): tag ids, `sportsMarketType` values,
start-time formats and `sportsradarMatchId` coverage per sport.
"""

from __future__ import annotations

from collections import Counter

from polybot.analytics.stats import md_table
from polybot.core.config import AppConfig
from polybot.core.http import make_client
from polybot.core.timeutil import now_ns, ns_to_iso
from polybot.venues.polymarket.gamma import GammaClient
from polybot.venues.polymarket.markets import ParseIssues, parse_event


async def run_discover(cfg: AppConfig, max_pages: int = 20) -> str:
    out: list[str] = []
    async with make_client(cfg.base.http) as http:
        gamma = GammaClient(http, cfg.base.polymarket.gamma_url, sink=None)
        sports = await gamma.get_sports()
        rows = [
            [s.get("id"), s.get("sport"), s.get("tags"), s.get("series")]
            for s in (sports if isinstance(sports, list) else [])
            if isinstance(s, dict)
        ]
        out += ["## /sports", md_table(["id", "sport", "tags", "series"], rows), ""]
        market_types = await gamma.get_market_types()
        out += ["## /sports/market-types", f"`{market_types}`", ""]
        now = now_ns()
        for sport, sport_cfg in cfg.recorder.sports.items():
            issues = ParseIssues()
            types: Counter[str] = Counter()
            n_events = n_matches = n_doubles = n_sr_id = 0
            samples: list[list[object]] = []
            for slug in sport_cfg.tag_slugs:
                tag_id = await gamma.resolve_tag_id(slug)
                page = await gamma.list_events(tag_id=tag_id, page_size=100, max_pages=max_pages)
                raws = page.events
                cut = " (усечено: увеличьте --max-pages)" if page.truncated else ""
                out.append(f"- {sport}: tag `{slug}` → id {tag_id}, open events: {len(raws)}{cut}")
                for raw in raws:
                    event = parse_event(raw, sport, issues)
                    n_events += 1
                    n_doubles += event.is_doubles
                    n_sr_id += event.sportsradar_match_id is not None
                    if not event.is_match:
                        continue
                    n_matches += 1
                    types.update(m.sports_market_type or "∅" for m in event.markets)
                    start = event.game_start_ns
                    if len(samples) < 8 and start is not None and start > now:
                        raw_start = next(
                            (m.game_start_raw for m in event.markets if m.game_start_raw), None
                        )
                        samples.append([event.title, event.slug, raw_start, ns_to_iso(start)])
            out += [
                "",
                f"## {sport}",
                f"events {n_events}, matches {n_matches}, doubles {n_doubles}, "
                f"with sportsradarMatchId {n_sr_id}; parse issues: {issues.as_dict()}",
                "",
                md_table(["sportsMarketType", "markets"], types.most_common()),
                "",
                md_table(["title", "slug", "gameStartTime (raw)", "UTC"], samples),
                "",
            ]
    return "\n".join(out)
