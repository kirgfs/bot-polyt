"""Can a $50 copy repeat a market maker? A short check of the top of the leaderboard by turnover.

A walk-forward backtest is impossible for them: the API returns only the 10 000 most recent fills [api_notes §3],
for a market maker that is hours. So the check uses what is available:
- the leaderboard: month PnL and turnover → profit per $1 of turnover;
- the trading address: the row's own address, or its biggest sub-account when the row sums sub-accounts
  [api_notes §6] (a copy bot can only follow one concrete address);
- its `portfolio` → drawdown over the month, and its most recent fills (`userFills`, up to 2000) → maker share,
  fills per day, fill sizes → what the copy bot would do with them.

All thresholds are the ones the scout already uses (config.yaml): market-maker detector, lost actions, taker fee,
the cheapest slippage tier. The copy cost is a lower bound: the copy-bot's own fee is not included.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from hl_scout.analytics import build_equity_curve, split_fills
from hl_scout.config import Config
from hl_scout.detectors import maker_stats
from hl_scout.util import DAY, HOUR, fmt_pct, fmt_usd, short_address


@dataclass
class FillStats:
    n: int
    fills_per_day: float
    maker_share: float
    median_fill_usd: float
    below_min_share: float  # share of their fills that become a copy order below the exchange minimum
    bumped_turnover_day_usd: float  # "Your Copy Size < $10" = buy $10: my daily turnover


@dataclass
class MmRow:
    address: str  # leaderboard row
    display_name: str | None
    trader: str  # the address that actually trades (the row itself or its sub-account)
    trader_note: str
    equity: float  # of the trading address: the copy ratio is taken against it
    month_vlm: float
    month_pnl: float
    max_dd_month: float | None
    last_fill: int | None
    stats: FillStats | None  # None: the API gives no fresh fills
    copy_ratio: float  # my deposit / their equity: the copy keeps their leverage
    copy_cost_bps: float  # lower bound per $1 of my turnover
    taker_fee_bps: float
    reasons: list[str] = field(default_factory=list)

    @property
    def edge_bps_month(self) -> float:
        return 1e4 * self.month_pnl / self.month_vlm if self.month_vlm > 0 else 0.0

    @property
    def gross_edge_bound_bps(self) -> float:
        """Their profit per $1 before fees is at most PnL + the taker fee (no one pays more than taker)."""
        return self.edge_bps_month + self.taker_fee_bps

    @property
    def median_copy_usd(self) -> float | None:
        return self.stats.median_fill_usd * self.copy_ratio if self.stats else None

    @property
    def bumped_fee_day_usd(self) -> float | None:
        return self.stats.bumped_turnover_day_usd * self.copy_cost_bps / 1e4 if self.stats else None

    @property
    def copyable(self) -> bool:
        return not self.reasons


def _perf(row: dict[str, Any], window: str, key: str) -> float:
    return float((row.get("perf", {}).get(window) or {}).get(key) or 0.0)


def select_top_turnover(rows: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: _perf(r, "month", "vlm"), reverse=True)[:count]


def pick_trader(sub_accounts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The most active sub-account right now: the biggest open notional, then the biggest account value."""

    def key(s: dict[str, Any]) -> tuple[float, float]:
        ms = (s.get("clearinghouseState") or {}).get("marginSummary") or {}
        try:
            return abs(float(ms.get("totalNtlPos") or 0)), float(ms.get("accountValue") or 0)
        except (TypeError, ValueError):
            return 0.0, 0.0

    return max(sub_accounts, key=key) if sub_accounts else None


def fill_stats(raw_fills: list[dict[str, Any]], address: str, equity: float, cfg: Config, now: int) -> FillStats | None:
    """Stats over the most recent fills (`userFills`, newest first, at most one page)."""
    fills, _ = split_fills(raw_fills, address, cfg.universe.allow_hip3)
    fills.sort(key=lambda f: f.t)
    if not fills or now - fills[-1].t > cfg.mm_check.stale_after_h * HOUR:
        return None
    # a full page ends at their newest fill; a partial one is all they have, up to now
    t1 = fills[-1].t + 1 if len(raw_fills) >= cfg.api.fills_page_max else now
    maker, per_day = maker_stats(fills, fills[0].t, t1)
    notional = np.array([f.notional for f in fills])
    copies = notional * (cfg.deposit.total_usd / equity if equity > 0 else 0.0)
    min_order = cfg.copying.min_order_usd
    return FillStats(
        n=len(fills),
        fills_per_day=per_day,
        maker_share=maker,
        median_fill_usd=float(np.median(notional)),
        below_min_share=float(np.mean(copies < min_order)),
        bumped_turnover_day_usd=float(np.mean(np.maximum(copies, min_order))) * per_day,
    )


def assess(
    row: dict[str, Any],
    trader: str,
    trader_note: str,
    equity: float,
    portfolio: Any,
    raw_fills: list[dict[str, Any]],
    cfg: Config,
    now: int,
) -> MmRow:
    curve = build_equity_curve(portfolio) if portfolio is not None else None
    dd = curve.max_drawdown(now - 30 * DAY, now) if curve is not None and not curve.empty else None
    times = [int(f["time"]) for f in raw_fills if "time" in f]
    out = MmRow(
        address=str(row["address"]),
        display_name=row.get("display_name"),
        trader=trader,
        trader_note=trader_note,
        equity=equity,
        month_vlm=_perf(row, "month", "vlm"),
        month_pnl=_perf(row, "month", "pnl"),
        max_dd_month=dd,
        last_fill=max(times) if times else None,
        stats=fill_stats(raw_fills, trader, equity, cfg, now),
        copy_ratio=cfg.deposit.total_usd / equity if equity > 0 else 0.0,
        copy_cost_bps=cfg.costs.taker_fee_bps + min(t.bps for t in cfg.costs.slippage_bps_tiers),
        taker_fee_bps=cfg.costs.taker_fee_bps,
    )
    dep, min_order = _usd(cfg.deposit.total_usd), _usd(cfg.copying.min_order_usd)
    st = out.stats
    if st is None:
        last = datetime.fromtimestamp(out.last_fill / 1000, tz=UTC).strftime("%Y-%m-%d") if out.last_fill else "нет"
        out.reasons.append(
            f"API не отдаёт свежие сделки этого адреса (последняя доступная: {last}), хотя по лидерборду оборот "
            "идёт каждый день. Проверить копию нельзя, а copy-бот, скорее всего, тоже не увидит его сделки"
        )
    else:
        mm = cfg.filters.market_maker
        if st.maker_share >= mm.maker_share and st.fills_per_day >= mm.fills_per_day:
            out.reasons.append(
                f"маркет-мейкер: {fmt_pct(st.maker_share, 0)} объёма лимитными ордерами, "
                f"{st.fills_per_day:,.0f} сделок в день. Их доход — спред и ребейты; копия входит через 10–30 с "
                "рыночным ордером и сама платит этот спред"
            )
        if st.below_min_share > cfg.recommend.max_lost_actions:
            out.reasons.append(
                f"{fmt_pct(st.below_min_share, 0)} их сделок в копии на {dep} меньше {min_order}: бот их пропустит, "
                f"копия не повторит позицию (порог {fmt_pct(cfg.recommend.max_lost_actions, 0)})"
            )
    if out.month_vlm > 0 and out.gross_edge_bound_bps < out.copy_cost_bps:
        out.reasons.append(
            f"прибыль на $1 оборота {out.edge_bps_month:.2f} б.п. за месяц, до комиссий — не больше "
            f"{out.gross_edge_bound_bps:.2f} б.п.; копия платит минимум {out.copy_cost_bps:.1f} б.п. "
            "(taker + проскальзывание). Копия в минусе, даже если бы входила по их ценам"
        )
    return out


def _usd(x: float) -> str:
    return f"${x:,.0f}" if float(x).is_integer() else fmt_usd(x)


def render(rows: list[MmRow], cfg: Config, now: int) -> str:
    date = datetime.fromtimestamp(now / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    dep, min_order = _usd(cfg.deposit.total_usd), _usd(cfg.copying.min_order_usd)
    bad = [r for r in rows if not r.copyable]
    dds = [r.max_dd_month for r in rows if r.max_dd_month is not None]
    lines = [
        f"# Маркет-мейкеры: можно ли их копировать на {dep}",
        "",
        f"{date}. Проверены {len(rows)} крупнейших по обороту за месяц строк лидерборда.",
        "",
        "## Коротко",
        "",
        f"- Не копируются: {len(bad)} из {len(rows)}.",
    ]
    if dds:
        lines.append(
            f"- Просадка за месяц у торгующих адресов: медиана {fmt_pct(float(np.median(dds)))}. "
            "Капитал её, может, и выдержал бы, но дело не в ней: копия не получает их доход."
        )
    if rows:
        lines.append(
            f"- Минимальные издержки копии — {rows[0].copy_cost_bps:.1f} б.п. с каждого $1 оборота "
            f"(taker {cfg.costs.taker_fee_bps:g} б.п. + проскальзывание), без комиссии copy-бота."
        )
    lines += [
        "- Строка лидерборда у крупных игроков — сумма мастер-аккаунта и его субаккаунтов. Сам мастер часто не "
        "торгует, а в лидерборде субаккаунтов нет. Поэтому ниже разобран адрес, который торгует на самом деле.",
        "",
        "| Строка лидерборда | Кто торгует | Капитал торгующего | Оборот за месяц | PnL за месяц | "
        "Прибыль на $1 оборота | Просадка за месяц | Лимитками | Сделок в день | Медианная сделка | "
        f"Она же в копии на {dep} | Копий < {min_order} | Если докупать до {min_order} |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        name = f" ({r.display_name})" if r.display_name else ""
        dd = fmt_pct(r.max_dd_month) if r.max_dd_month is not None else "—"
        st = r.stats
        if st is None:
            fills = "нет свежих сделок в API | — | — | — | — | —"
        else:
            fills = (
                f"{fmt_pct(st.maker_share, 0)} | {st.fills_per_day:,.0f} | {fmt_usd(st.median_fill_usd)} | "
                f"{fmt_usd(r.median_copy_usd or 0.0)} | {fmt_pct(st.below_min_share, 0)} | "
                f"оборот {fmt_usd(st.bumped_turnover_day_usd)}/день, издержки ≥ {fmt_usd(r.bumped_fee_day_usd or 0)}"
                "/день"
            )
        lines.append(
            f"| `{short_address(r.address)}`{name} | {r.trader_note} | {fmt_usd(r.equity)} | {fmt_usd(r.month_vlm)} | "
            f"{fmt_usd(r.month_pnl)} | {r.edge_bps_month:.2f} б.п. | {dd} | {fills} |"
        )
    lines += ["", "## Почему", ""]
    for r in rows:
        who = "" if r.trader == r.address else f" → торгует `{r.trader}`"
        lines.append(f"**`{r.address}`**" + (f" ({r.display_name})" if r.display_name else "") + who)
        lines += [f"- {x}" for x in r.reasons] or ["- Экономика копии не против: нужен полный бэктест (`check`)."]
        lines.append("")
    lines += [
        "## Как считалось",
        "",
        "- Прибыль на $1 оборота = PnL за месяц / оборот за месяц из строки лидерборда (1 б.п. = 0.01%). До комиссий "
        f"она не больше этого плюс taker {cfg.costs.taker_fee_bps:g} б.п.: дороже taker никто не платит.",
        "- Допущение: копия с задержкой в среднем входит не лучше трейдера, поэтому её доход до комиссий не выше.",
        f"- Копия сделки = размер их сделки × {dep} / капитал торгующего адреса: копия держит то же плечо.",
        f"- «Если докупать до {min_order}» — настройка «Your Copy Size < $10 → купить $10»: каждая мелкая сделка "
        "становится ордером $10, оборот и издержки считаются по их числу сделок в день.",
        "- Доля лимиток, сделки в день и размеры — по последним филлам торгующего адреса (`userFills`, до 2000).",
        "- Полный walk-forward для них невозможен: API отдаёт только 10 000 последних филлов, у них это часы.",
    ]
    return "\n".join(lines) + "\n"


def to_json(rows: list[MmRow]) -> list[dict[str, Any]]:
    return [
        {
            **asdict(r),
            "edge_bps_month": r.edge_bps_month,
            "median_copy_usd": r.median_copy_usd,
            "bumped_fee_day_usd": r.bumped_fee_day_usd,
            "copyable": r.copyable,
        }
        for r in rows
    ]
