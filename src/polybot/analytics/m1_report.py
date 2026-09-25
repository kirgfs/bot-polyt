"""M1 data report after the recording week (user decision: M1 ends with this report).

Sections: data quality, markets, traded volume and spreads by sport and time to start,
network and feed latency, early starts and exchange auto-cancel, OddsPapi quality and
matching accuracy, fees/delays/rewards. Heavy lifting runs in DuckDB over the raw store.

Every number here is descriptive. Heuristics are labelled as such in the output.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import duckdb

from polybot.analytics.loaders import iter_recorded_odds, load_pm_events
from polybot.analytics.oddspapi_quality import (
    load_matching_context,
    sharp_observations,
    summary_tables,
)
from polybot.analytics.stats import md_table, summarize
from polybot.core.config import AppConfig, Settings
from polybot.core.timeutil import NS_PER_S, now_ns, ns_to_date, ns_to_iso
from polybot.data.records import Source
from polybot.data.store import connect, query, scan
from polybot.venues.polymarket.markets import PmEvent

NS_PER_H = 3600 * NS_PER_S
# Time-to-start buckets, hours before the scheduled start (negative = after it).
BUCKETS: tuple[tuple[str, float, float], ...] = (
    (">24ч", 24.0, 1e9),
    ("24–6ч", 6.0, 24.0),
    ("6–1ч", 1.0, 6.0),
    ("1ч–старт", 0.0, 1.0),
    ("после старта", -1e9, 0.0),
)
# A top-of-book value is held at most this long; longer gaps are recorder downtime.
MAX_HOLD_NS = 30 * 60 * NS_PER_S
# Feed-latency heuristic: a mid move of at least this many ticks within ±15 s of a score change.
REACTION_TICKS = 2
REACTION_WINDOW_NS = 15 * NS_PER_S


def bucket_of(tts_ns: int) -> str:
    hours = tts_ns / NS_PER_H
    for name, low, high in BUCKETS:
        if low < hours <= high:
            return name
    return BUCKETS[-1][0]


@dataclass(frozen=True, slots=True)
class AssetInfo:
    sport: str
    start_ns: int
    tick: float
    game_id: str | None
    condition_id: str
    outcome_index: int


def asset_map(
    events: list[PmEvent], market_types: dict[str, tuple[str, ...]]
) -> dict[str, AssetInfo]:
    assets: dict[str, AssetInfo] = {}
    for event in events:
        for market in event.markets:
            if market.sports_market_type not in market_types.get(event.sport, ()):
                continue
            if market.game_start_ns is None or not market.is_binary_with_tokens:
                continue
            tick = float(market.tick_size) if market.tick_size is not None else 0.01
            for index, token in enumerate(market.token_ids):
                assets[token] = AssetInfo(
                    event.sport,
                    market.game_start_ns,
                    tick,
                    event.game_id,
                    market.condition_id,
                    index,
                )
    return assets


def _register_assets(con: duckdb.DuckDBPyConnection, assets: dict[str, AssetInfo]) -> None:
    con.execute(
        "CREATE OR REPLACE TEMP TABLE assets (asset_id VARCHAR, sport VARCHAR, start_ns BIGINT, "
        "tick DOUBLE, game_id VARCHAR, condition_id VARCHAR, outcome_index INTEGER)"
    )
    if assets:
        con.executemany(
            "INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                [a, i.sport, i.start_ns, i.tick, i.game_id, i.condition_id, i.outcome_index]
                for a, i in assets.items()
            ],
        )


# ---------------------------------------------------------------------------- sections


def section_quality(con: duckdb.DuckDBPyConnection, root: Path, since: int) -> str:
    rows = []
    for source in Source:
        table = scan(root, source.value)
        if table is None:
            continue
        stats = query(
            con,
            f"SELECT count(*), sum(length(payload)), min(ts_recv_ns), max(ts_recv_ns), "
            f"count(DISTINCT run_id) FROM {table} WHERE ts_recv_ns >= ?",
            [since],
        )[0]
        if stats[0]:
            rows.append(
                [
                    source.value,
                    stats[0],
                    f"{(stats[1] or 0) / 1e6:.1f}",
                    ns_to_iso(stats[2])[:16],
                    ns_to_iso(stats[3])[:16],
                    stats[4],
                ]
            )
    out = [
        "### Объём записи",
        md_table(["источник", "строк", "МБ (сырой текст)", "с", "по", "запусков"], rows),
    ]
    ws = scan(root, Source.CLOB_MARKET_WS.value)
    if ws is not None:
        controls = query(
            con,
            f"SELECT event_type, count(*) FROM {ws} WHERE kind = 'control' AND ts_recv_ns >= ? "
            "GROUP BY 1 ORDER BY 2 DESC",
            [since],
        )
        desyncs = query(
            con,
            f"SELECT json_extract_string(payload, '$.reason'), count(*) FROM {ws} "
            "WHERE kind = 'control' AND event_type = 'desync' AND ts_recv_ns >= ? GROUP BY 1",
            [since],
        )
        out += [
            "",
            "### Market WS: служебные события",
            md_table(["событие", "число"], controls),
            "",
            "Рассинхроны книги по причинам (после каждого — переподписка):",
            md_table(["причина", "число"], desyncs),
        ]
    rest = scan(root, Source.CLOB_REST_BOOKS.value)
    if rest is not None:
        n = query(
            con, f"SELECT count(*), sum(n_events) FROM {rest} WHERE ts_recv_ns >= ?", [since]
        )[0]
        out += ["", f"Сверок с REST `/books`: {n[0]} запросов, {n[1] or 0} книг."]
    return "\n".join(out)


def section_markets(events: list[PmEvent], assets: dict[str, AssetInfo]) -> str:
    per_sport: dict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        per_sport[event.sport]["событий"] += 1
        per_sport[event.sport]["парных"] += event.is_doubles
        per_sport[event.sport]["с sportsradarMatchId"] += event.sportsradar_match_id is not None
        per_sport[event.sport]["с gameId"] += event.game_id is not None
    for info in assets.values():
        per_sport[info.sport]["токенов в записи"] += 1
    keys = ["событий", "парных", "с sportsradarMatchId", "с gameId", "токенов в записи"]
    rows = [[sport, *(c[k] for k in keys)] for sport, c in sorted(per_sport.items())]
    return md_table(["вид", *keys], rows)


def section_volume(con: duckdb.DuckDBPyConnection, root: Path, since: int) -> str:
    ws = scan(root, Source.CLOB_MARKET_WS.value)
    if ws is None:
        return "Нет данных market WS."
    rows = query(
        con,
        f"""
        WITH trades AS (
            SELECT ts_recv_ns,
                   json_extract_string(payload, '$.asset_id') AS asset_id,
                   TRY_CAST(json_extract_string(payload, '$.price') AS DOUBLE) AS price,
                   TRY_CAST(json_extract_string(payload, '$.size') AS DOUBLE) AS size
            FROM {ws}
            WHERE kind = 'frame' AND event_type = 'last_trade_price' AND n_events = 1
              AND ts_recv_ns >= ?
        )
        SELECT a.sport, (a.start_ns - t.ts_recv_ns) AS tts, t.price, t.size
        FROM trades t JOIN assets a USING (asset_id)
        WHERE t.price IS NOT NULL AND t.size IS NOT NULL
        """,
        [since],
    )
    shares: dict[tuple[str, str], float] = defaultdict(float)
    notional: dict[tuple[str, str], float] = defaultdict(float)
    count: dict[tuple[str, str], int] = defaultdict(int)
    for sport, tts, price, size in rows:
        key = (sport, bucket_of(int(tts)))
        shares[key] += size
        notional[key] += size * price
        count[key] += 1
    table = [
        [
            sport,
            bucket,
            count[(sport, bucket)],
            f"{shares[(sport, bucket)]:,.0f}",
            f"{notional[(sport, bucket)]:,.0f}",
        ]
        for sport in sorted({k[0] for k in count})
        for bucket, _, _ in BUCKETS
        if (sport, bucket) in count
    ]
    return (
        md_table(["вид", "до старта", "сделок", "акций", "нотионал, $"], table)
        + "\n\nСделки — события `last_trade_price` по записанным токенам. Обе стороны "
        "бинарного рынка учитываются отдельно: при зеркальных книгах одна сделка может прийти "
        "по обоим токенам (см. раздел о зеркальности)."
    )


def build_top_of_book(con: duckdb.DuckDBPyConnection, root: Path, since: int) -> bool:
    """TEMP TABLE tob(asset_id, ts, bid, ask, next_ts) from best_bid/best_ask in price_change."""
    ws = scan(root, Source.CLOB_MARKET_WS.value)
    if ws is None:
        return False
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE tob AS
        WITH changes AS (
            SELECT ts_recv_ns AS ts,
                   unnest(from_json(json_extract(payload, '$.price_changes'),
                          '[{{"asset_id":"VARCHAR","best_bid":"VARCHAR","best_ask":"VARCHAR"}}]'),
                          recursive := true)
            FROM {ws}
            WHERE kind = 'frame' AND event_type = 'price_change' AND n_events = 1
              AND ts_recv_ns >= {int(since)}
        ), per_asset AS (
            SELECT asset_id, ts,
                   max(CASE WHEN best_bid IN ('', '0') THEN NULL ELSE TRY_CAST(best_bid AS DOUBLE) END) AS bid,
                   max(CASE WHEN best_ask IN ('', '1') THEN NULL ELSE TRY_CAST(best_ask AS DOUBLE) END) AS ask
            FROM changes GROUP BY asset_id, ts
        )
        SELECT p.*, lead(ts) OVER (PARTITION BY asset_id ORDER BY ts) AS next_ts
        FROM per_asset p JOIN assets USING (asset_id)
        """  # noqa: E501
    )
    return True


def section_spreads(con: duckdb.DuckDBPyConnection) -> str:
    rows = query(
        con,
        f"""
        SELECT a.sport, (a.start_ns - t.ts) AS tts, a.tick, t.ask - t.bid AS spread,
               least(coalesce(t.next_ts, t.ts) - t.ts, {MAX_HOLD_NS}) AS hold
        FROM tob t JOIN assets a USING (asset_id)
        WHERE t.bid IS NOT NULL AND t.ask IS NOT NULL
        """,
    )
    weighted: dict[tuple[str, str], float] = defaultdict(float)
    ticks: dict[tuple[str, str], float] = defaultdict(float)
    one_tick: dict[tuple[str, str], float] = defaultdict(float)
    total: dict[tuple[str, str], float] = defaultdict(float)
    for sport, tts, tick, spread, hold in rows:
        if hold <= 0:
            continue
        key = (sport, bucket_of(int(tts)))
        weighted[key] += spread * hold
        ticks[key] += (spread / tick) * hold
        one_tick[key] += hold if spread <= tick * 1.0001 else 0.0
        total[key] += hold
    table = [
        [
            sport,
            bucket,
            f"{100 * weighted[(sport, bucket)] / total[(sport, bucket)]:.2f}",
            f"{ticks[(sport, bucket)] / total[(sport, bucket)]:.1f}",
            f"{one_tick[(sport, bucket)] / total[(sport, bucket)]:.0%}",
            f"{total[(sport, bucket)] / NS_PER_H:,.0f}",
        ]
        for sport in sorted({k[0] for k in total})
        for bucket, _, _ in BUCKETS
        if total.get((sport, bucket))
    ]
    return (
        md_table(
            [
                "вид",
                "до старта",
                "спред, ¢ (взвеш. по времени)",
                "в тиках",
                "доля времени спред = 1 тик",
                "токен-часов",
            ],
            table,
        )
        + "\n\nСпред — `best_ask − best_bid` из сообщений `price_change`; значение держится до "
        "следующего изменения, не дольше 30 мин."
    )


def section_network(con: duckdb.DuckDBPyConnection, root: Path, since: int) -> str:
    rows = []
    probe = scan(root, Source.PROBE_REST.value)
    if probe is not None:
        values = [
            r[0] / 1e6
            for r in query(
                con,
                f"SELECT latency_ns FROM {probe} "
                "WHERE event_type = 'clob_time' AND ts_recv_ns >= ?",
                [since],
            )
        ]
        rows.append(
            ["REST GET /time (раз в минуту, тёплое соединение), мс", *summarize(values).row()]
        )
    ws = scan(root, Source.CLOB_MARKET_WS.value)
    if ws is not None:
        values = [
            r[0] / 1e6
            for r in query(
                con,
                f"SELECT latency_ns FROM {ws} WHERE kind = 'probe' AND ts_recv_ns >= ?",
                [since],
            )
        ]
        rows.append(["WS market PING→PONG, мс", *summarize(values).row()])
        values = [
            float(r[0])
            for r in query(
                con,
                f"SELECT ts_recv_ns / 1e6 - server_ts_ms FROM {ws} WHERE kind = 'frame' "
                "AND event_type IN ('price_change', 'last_trade_price', 'best_bid_ask') "
                "AND server_ts_ms IS NOT NULL AND ts_recv_ns >= ? USING SAMPLE 200000 ROWS",
                [since],
            )
        ]
        rows.append(["WS market: сервер `timestamp` → получение, мс", *summarize(values).row()])
    return md_table(["метрика", "n", "p50", "p95", "p99", "min", "max"], rows) + (
        "\n\nOne-way зависит от синхронизации часов (chrony на VPS, NTP у биржи)."
    )


@dataclass
class ScoreChange:
    game_id: str
    ts: int
    score: str


def score_changes(con: duckdb.DuckDBPyConnection, root: Path, since: int) -> list[ScoreChange]:
    sports = scan(root, Source.SPORTS_WS.value)
    if sports is None:
        return []
    rows = query(
        con,
        f"SELECT key, ts_recv_ns, json_extract_string(payload, '$.score') FROM {sports} "
        "WHERE kind = 'frame' AND key IS NOT NULL AND ts_recv_ns >= ? ORDER BY key, ts_recv_ns",
        [since],
    )
    changes: list[ScoreChange] = []
    last: dict[str, str | None] = {}
    for game_id, ts, score in rows:
        if game_id in last and score != last[game_id] and score:
            changes.append(ScoreChange(str(game_id), int(ts), str(score)))
        last[game_id] = score
    return changes


def section_feed_latency(con: duckdb.DuckDBPyConnection, root: Path, since: int) -> str:
    changes = score_changes(con, root, since)
    if not changes:
        return "Нет смен счёта в Sports WS за период."
    con.execute("CREATE OR REPLACE TEMP TABLE score_changes (game_id VARCHAR, ts BIGINT)")
    con.executemany("INSERT INTO score_changes VALUES (?, ?)", [[c.game_id, c.ts] for c in changes])
    rows = query(
        con,
        f"""
        WITH moves AS (
            SELECT t.asset_id, t.ts, a.game_id, a.sport,
                   abs((t.bid + t.ask) / 2 - lag((t.bid + t.ask) / 2)
                       OVER (PARTITION BY t.asset_id ORDER BY t.ts)) / a.tick AS move_ticks
            FROM tob t JOIN assets a USING (asset_id)
            WHERE t.bid IS NOT NULL AND t.ask IS NOT NULL AND a.game_id IS NOT NULL
        )
        SELECT s.game_id, s.ts, any_value(m.sport), min(m.ts)
        FROM score_changes s
        JOIN moves m ON m.game_id = s.game_id
         AND m.ts BETWEEN s.ts - {REACTION_WINDOW_NS} AND s.ts + {REACTION_WINDOW_NS}
         AND m.move_ticks >= {REACTION_TICKS}
        GROUP BY s.game_id, s.ts
        """,
    )
    by_sport: dict[str, list[float]] = defaultdict(list)
    for _game, ts_score, sport, ts_react in rows:
        by_sport[str(sport)].append((ts_score - ts_react) / 1e9)
    table = [
        [sport, *summarize(values).row("{:+.2f}")] for sport, values in sorted(by_sport.items())
    ]
    examples = Counter(c.score for c in changes[:2000]).most_common(8)
    return (
        md_table(["вид", "n", "p50, с", "p95, с", "p99, с", "min", "max"], table)
        + f"\n\nСмен счёта всего: {len(changes)}, с найденной реакцией книги: {len(rows)}. "
        "**Эвристика** (docs/data_sources.md §4.2): Δ = время кадра Sports WS − первое движение "
        f"мида ≥ {REACTION_TICKS} тиков в окне ±15 с. Δ > 0 — фид позже рынка; это оценка снизу "
        "задержки «очко → кадр».\n\nПримеры строк `score`: "
        + ", ".join(f"`{s}`" for s, _ in examples)
    )


def section_starts(
    con: duckdb.DuckDBPyConnection, root: Path, since: int, events: list[PmEvent]
) -> str:
    sports = scan(root, Source.SPORTS_WS.value)
    if sports is None:
        return "Нет данных Sports WS."
    first_live = dict(
        query(
            con,
            f"SELECT key, min(ts_recv_ns) FROM {sports} "
            "WHERE kind = 'frame' AND key IS NOT NULL "
            "AND json_extract(payload, '$.live')::VARCHAR = 'true' AND ts_recv_ns >= ? "
            "GROUP BY key",
            [since],
        )
    )
    empty_book = dict(
        query(
            con,
            "SELECT a.game_id, min(t.ts) FROM tob t JOIN assets a USING (asset_id) "
            "WHERE t.bid IS NULL AND t.ask IS NULL AND a.game_id IS NOT NULL "
            "AND t.ts BETWEEN a.start_ns - 3600000000000 AND a.start_ns + 7200000000000 "
            "GROUP BY a.game_id",
        )
    )
    early: dict[str, list[float]] = defaultdict(list)
    cancel_vs_sched: dict[str, list[float]] = defaultdict(list)
    for event in events:
        start = event.game_start_ns
        if event.game_id is None or start is None:
            continue
        live = first_live.get(event.game_id)
        if live is not None:
            early[event.sport].append((live - start) / 60e9)
        cancel = empty_book.get(event.game_id)
        if cancel is not None:
            cancel_vs_sched[event.sport].append((cancel - start) / 60e9)
    rows = []
    for sport in sorted(early):
        values = early[sport]
        n_early = sum(1 for v in values if v < -5)
        rows.append(
            [sport, *summarize(values).row("{:+.1f}"), f"{n_early} ({n_early / len(values):.0%})"]
        )
    cancel_rows = [[s, *summarize(v).row("{:+.1f}")] for s, v in sorted(cancel_vs_sched.items())]
    return (
        "Старт по Sports WS (первый кадр с `live=true`) минус заявленный `gameStartTime`, "
        "минуты:\n\n"
        + md_table(["вид", "n", "p50", "p95", "p99", "min", "max", "раньше >5 мин"], rows)
        + "\n\nПервая пустая книга (обе стороны) относительно `gameStartTime`, минуты — оценка "
        "момента авто-отмены биржей (чек-лист §16, п. 1):\n\n"
        + md_table(["вид", "n", "p50", "p95", "p99", "min", "max"], cancel_rows)
        + "\n\nУсловие «предыдущий матч на том же корте завершён» не измеряется: в Sports WS и "
        "OddsPapi (по известным полям) нет корта. Это открытый вопрос риска B1."
    )


def section_meta(events: list[PmEvent]) -> str:
    delays: dict[str, Counter[str]] = defaultdict(Counter)
    ticks: dict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        for market in event.markets:
            delays[event.sport][str(market.seconds_delay)] += 1
            ticks[event.sport][str(market.tick_size)] += 1
    rows = [
        [
            sport,
            ", ".join(f"{k}: {v}" for k, v in delays[sport].most_common(5)),
            ", ".join(f"{k}: {v}" for k, v in ticks[sport].most_common(5)),
        ]
        for sport in sorted(delays)
    ]
    return md_table(["вид", "secondsDelay (значение: рынков)", "тик (значение: рынков)"], rows)


def section_mirror(con: duckdb.DuckDBPyConnection) -> str:
    """Checklist §16 item 13: bid(YES) = 1 − ask(NO) at the same message time."""
    rows = query(
        con,
        """
        SELECT count(*),
               sum(CASE WHEN abs(y.bid - (1 - n.ask)) < 1e-9 AND abs(y.ask - (1 - n.bid)) < 1e-9
                        THEN 1 ELSE 0 END)
        FROM tob y
        JOIN assets ay ON ay.asset_id = y.asset_id AND ay.outcome_index = 0
        JOIN assets an ON an.condition_id = ay.condition_id AND an.outcome_index = 1
        JOIN tob n ON n.asset_id = an.asset_id AND n.ts = y.ts
        WHERE y.bid IS NOT NULL AND y.ask IS NOT NULL AND n.bid IS NOT NULL AND n.ask IS NOT NULL
        """,
    )
    total, mirrored = rows[0]
    if not total:
        return "Нет пар YES/NO с одновременными изменениями."
    share = mirrored / total
    return f"Одновременных изменений пары YES/NO: {total}, зеркальных: {mirrored} ({share:.1%})."


def _num(value: object) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def section_rewards(
    con: duckdb.DuckDBPyConnection, root: Path, since: int, assets: dict[str, AssetInfo]
) -> str:
    """Liquidity reward pools of the recorded markets (/rewards/markets/current, hourly)."""
    table = scan(root, Source.CLOB_REWARDS.value)
    if table is None:
        return "Нет записей `/rewards/markets/current`."
    sport_of = {info.condition_id: info.sport for info in assets.values()}
    daily: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    max_spread: dict[str, dict[str, float]] = defaultdict(dict)
    min_size: dict[str, dict[str, float]] = defaultdict(dict)
    rows = query(
        con,
        f"SELECT ts_recv_ns, payload FROM {table} "
        "WHERE kind = 'rest' AND status = 200 AND ts_recv_ns >= ?",
        [since],
    )
    for ts, payload in rows:
        data = json.loads(payload)
        items = data.get("data") if isinstance(data, dict) else None
        day = ns_to_date(int(ts)).isoformat()
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("condition_id") or "")
            sport = sport_of.get(cid)
            if sport is None:
                continue
            rate = _num(item.get("total_daily_rate"))
            if rate is None:
                configs = item.get("rewards_config") or []
                rate = sum(
                    _num(c.get("rate_per_day")) or 0.0 for c in configs if isinstance(c, dict)
                )
            bucket = daily[(sport, day)]
            bucket[cid] = max(bucket.get(cid, 0.0), rate)
            if (spread := _num(item.get("rewards_max_spread"))) is not None:
                max_spread[sport][cid] = spread
            if (size := _num(item.get("rewards_min_size"))) is not None:
                min_size[sport][cid] = size
    recorded: dict[str, set[str]] = defaultdict(set)
    for info in assets.values():
        recorded[info.sport].add(info.condition_id)
    out = []
    for sport in sorted(recorded):
        days = {d: rates for (s, d), rates in daily.items() if s == sport}
        with_rewards = set().union(*(set(r) for r in days.values())) if days else set()
        per_day = [sum(r.values()) for r in days.values()]
        share = len(with_rewards) / len(recorded[sport])
        out.append(
            [
                sport,
                len(recorded[sport]),
                f"{len(with_rewards)} ({share:.0%})",
                f"{sum(per_day) / len(per_day):,.0f}" if per_day else "—",
                f"{sum(per_day):,.0f}" if per_day else "—",
                f"{summarize(max_spread[sport].values()).p50:g}" if max_spread[sport] else "—",
                f"{summarize(min_size[sport].values()).p50:g}" if min_size[sport] else "—",
            ]
        )
    header = [
        "вид",
        "рынков в записи",
        "с наградами",
        "$/день (в среднем)",
        "$ за период",
        "rewards_max_spread (медиана)",
        "rewards_min_size (медиана)",
    ]
    return (
        md_table(header, out)
        + "\n\nСтавки — `total_daily_rate` (или сумма `rate_per_day`) по рынкам, которые пишет "
        "рекордер. Единицы `rewards_max_spread` не задокументированы, выводятся как есть. "
        "Для этапа 0 это главный источник дохода, если его хватает."
    )


def section_sharp_gap(cfg: AppConfig, con: duckdb.DuckDBPyConnection, root: Path) -> str:
    """How far Polymarket's mid is from Pinnacle's no-vig price (stage-0 decision input)."""
    context = load_matching_context(cfg, root)
    observations = sharp_observations(context, iter_recorded_odds(con, root))
    if not observations:
        return (
            "Нет пар «цена Pinnacle + сопоставленный рынок Polymarket» "
            "(нужны шаги `oddspapi-eval`)."
        )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE sharp (sport VARCHAR, asset_id VARCHAR, start_ns BIGINT, "
        "ts BIGINT, prob DOUBLE)"
    )
    con.executemany(
        "INSERT INTO sharp VALUES (?, ?, ?, ?, ?)",
        [[o.sport, o.asset_id, o.start_ns, o.ts_recv_ns, o.prob] for o in observations],
    )
    rows = query(
        con,
        f"""
        SELECT s.sport, s.start_ns - s.ts AS tts, (t.bid + t.ask) / 2 - s.prob AS gap
        FROM sharp s ASOF JOIN tob t ON s.asset_id = t.asset_id AND s.ts >= t.ts
        WHERE t.bid IS NOT NULL AND t.ask IS NOT NULL AND s.ts - t.ts <= {MAX_HOLD_NS}
        """,
    )
    gaps: dict[tuple[str, str], list[float]] = defaultdict(list)
    for sport, tts, gap in rows:
        gaps[(sport, bucket_of(int(tts)))].append(100.0 * gap)
    table = []
    for (sport, bucket), values in sorted(gaps.items()):
        absolute = summarize(abs(v) for v in values)
        over = sum(1 for v in values if abs(v) > 2.0)
        table.append(
            [
                sport,
                bucket,
                absolute.n,
                f"{absolute.p50:.2f}",
                f"{absolute.p95:.2f}",
                f"{over / len(values):.0%}",
                f"{sum(values) / len(values):+.2f}",
            ]
        )
    header = [
        "вид",
        "до старта",
        "n",
        "|разрыв| p50, ¢",
        "|разрыв| p95, ¢",
        "доля > 2¢",
        "средний знак, ¢",
    ]
    return (
        md_table(header, table)
        + f"\n\nНаблюдений всего: {len(observations)}, с книгой Polymarket рядом по времени: "
        f"{len(rows)}. Разрыв = середина книги Polymarket − вероятность Pinnacle без маржи "
        "(простое нормирование; только двусторонние рынки с подтверждёнными id исходов — пока "
        "теннис). Малый разрыв — цены Polymarket и так близки к острым, этап 0 без фида имеет "
        "шанс; большой и частый — за платный фид есть за что платить."
    )


def build_report(settings: Settings, cfg: AppConfig, *, days: float = 7.0) -> str:
    root = settings.data_dir / "raw"
    since = now_ns() - int(days * 24 * NS_PER_H)
    con = connect()
    events, issues = load_pm_events(con, root, since_ns=since)
    market_types = {str(s): c.market_types for s, c in cfg.recorder.sports.items()}
    assets = asset_map(events, market_types)
    _register_assets(con, assets)
    have_tob = build_top_of_book(con, root, since)
    parts = [
        f"# Данные M1: отчёт за {days:g} дн. (сгенерирован {ns_to_iso(now_ns())[:16]} UTC)",
        "",
        "## 1. Качество записи",
        section_quality(con, root, since),
        "",
        "## 2. Рынки",
        section_markets(events, assets),
        f"\nПроблемы разбора Gamma: {json.dumps(issues.as_dict(), ensure_ascii=False)}",
        "",
        "## 3. Объём сделок по видам спорта",
        section_volume(con, root, since),
        "",
        "## 4. Спреды",
        section_spreads(con) if have_tob else "Нет данных.",
        "",
        "## 5. Задержки сети",
        section_network(con, root, since),
        "",
        "## 6. Задержка фида счёта (Sports WS)",
        section_feed_latency(con, root, since) if have_tob else "Нет данных.",
        "",
        "## 7. Ранние старты и авто-отмена",
        section_starts(con, root, since, events) if have_tob else "Нет данных.",
        "",
        "## 8. Параметры рынков",
        section_meta(events),
        "",
        "## 9. Зеркальность книг YES/NO",
        section_mirror(con) if have_tob else "Нет данных.",
        "",
        "## 10. Награды за ликвидность",
        section_rewards(con, root, since, assets),
        "",
        "## 11. Цена Polymarket против Pinnacle",
        section_sharp_gap(cfg, con, root) if have_tob else "Нет данных.",
        "",
        "## 12. OddsPapi и точность сопоставления",
        _oddspapi_section(cfg, root),
    ]
    return "\n".join(parts)


def _oddspapi_section(cfg: AppConfig, root: Path) -> str:
    if scan(root, Source.ODDSPAPI_REST.value) is None:
        return "OddsPapi не записывался (нет `oddspapi_rest`)."
    return summary_tables(cfg, root)
