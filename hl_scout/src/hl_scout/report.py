"""Human report (Markdown, Russian) and a JSON dump of a ScoutRun."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hl_scout.backtest import PROFILE_RU, PROFILES, ProfileSummary, WalletBacktest
from hl_scout.montecarlo import McResult
from hl_scout.pipeline import ScoutRun
from hl_scout.scoring import WalletEval
from hl_scout.sim import CopySettings
from hl_scout.util import explorer_url, fmt_pct, fmt_usd, short_address


def _d(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def _p(x: float | None, digits: int = 1) -> str:
    if x is None or not math.isfinite(x):
        return "—"
    if 0 < x < 0.001:
        return "<0.1%"
    return fmt_pct(x, digits)


def _f(x: float | None, fmt: str = "{:.2f}") -> str:
    if x is None or not math.isfinite(x):
        return "—"
    return fmt.format(x)


def _mc_line(mc: McResult | None, levels: list[float]) -> str:
    if mc is None:
        return "нет данных"
    ge = ", ".join(f"P(≥ {fmt_usd(lv)}) {_p(mc.p_ge.get(lv))}" for lv in levels)
    warn = "" if mc.reliable else " ⚠ мало дней в выборке"
    return (
        f"медиана {fmt_usd(mc.median)}, диапазон 5–95%: {fmt_usd(mc.p5)} … {fmt_usd(mc.p95)}; {ge}; "
        f"P(потеря > {mc.loss_threshold:.0%}) {_p(mc.p_loss)}; P(обнуление) {_p(mc.p_ruin)}{warn}"
    )


def settings_rows(s: CopySettings) -> list[tuple[str, str]]:
    """Copy-bot settings in the bot's own field names (semantics: copybot_fields.yaml, still unverified)."""
    return [
        ("Copy Ratio", f"{s.copy_ratio:g}x (цель ≈ {fmt_usd(s.target_usd)} на позицию)"),
        ("Min Trade Size", fmt_usd(s.min_trade_usd)),
        ("Max Trade Size", fmt_usd(s.max_trade_usd) if s.max_trade_usd else "-"),
        ("Buy Times Per Token", str(s.buy_times) if s.buy_times else "без ограничения"),
        ("Your Copy Size < $10", "Buy" if s.small_size == "buy" else "Skip"),
        ("Max number of tokens", str(s.max_tokens) if s.max_tokens else "-"),
        (
            "Max Token Size / Max Token Margin",
            f"{fmt_usd(s.max_token_size_usd or 0)} / {fmt_usd(s.max_token_margin_usd or 0)}",
        ),
        ("Плечо (фикс.)", f"{s.leverage}x"),
        ("Max Total Margin", fmt_usd(s.max_total_margin_usd or 0)),
        ("Price SL", _p(s.price_sl_pct) if s.price_sl_pct else "-"),
        ("Price TP", "-"),
        ("Balance SL", fmt_usd(s.balance_sl_usd) if s.balance_sl_usd else "-"),
        ("Copy LONG / SHORT", f"{'ON' if s.copy_long else 'OFF'} / {'ON' if s.copy_short else 'OFF'}"),
    ]


def _profile_block(p: ProfileSummary, levels: list[float]) -> list[str]:
    name = PROFILE_RU[p.profile]
    out = [f"**Профиль «{name}»** — {'✅ проходит' if p.eligible else '❌ не рекомендуется'}"]
    if p.settings is not None:
        out.append("")
        out.append("| Поле copy-бота | Значение |")
        out.append("|---|---|")
        out += [f"| {k} | {v} |" for k, v in settings_rows(p.settings)]
        if p.thresholds is not None:
            th = p.thresholds
            out.append(
                f"| Правила ПРЕКРАТИТЬ (подобраны на обучающем окне) | копия −{th.copy_dd:.0%}; просадка кошелька "
                f"> {th.dd7_mult:g}× нормы; {th.consecutive_losses} убыточных подряд{'; пауза в экстремальном режиме' if p.pause else ''} |"
            )
        out.append("")
    out.append(f"- Прогноз на 30 дней (Monte Carlo 10 000, out-of-sample): {_mc_line(p.mc, levels)}")
    out.append(f"- Тестовых окон с копированием: {p.windows}, прибыльных: {p.profitable} ({_p(p.profitable_share, 0)})")
    if p.reject:
        out.append("- Почему нет: " + "; ".join(p.reject))
    out.append("")
    return out


def _style(e: WalletEval) -> str:
    m = e.metrics
    coins = ", ".join(f"{c} {v:.0%}" for c, v in list(e.style.get("coin_shares", {}).items())[:5])
    lev_now = e.style.get("current_leverage") or {}
    lev_now_s = ", ".join(f"{c} {v}x" for c, v in lev_now.items()) if lev_now else "нет позиций"
    return (
        f"монеты: {coins}; лонг {m['long_share']:.0%} / шорт {1 - m['long_share']:.0%}; удержание среднее "
        f"{m['avg_hold_min'] / 60:.1f} ч (медиана {m['median_hold_min'] / 60:.1f} ч); {m['trades_per_week']:.1f} сделок/нед; "
        f"эфф. плечо медиана {m['leverage_median']:.1f}x (p90 {m['leverage_p90']:.1f}x); сейчас: {lev_now_s}"
    )


def _why(e: WalletEval) -> list[str]:
    m = e.metrics
    return [
        f"Deflated Sharpe {e.dsr:.2f} (вероятность, что это навык, а не лучший из {''}случайных), bootstrap q = {e.qvalue:.3f}",
        f"Sortino {m['sortino']:.2f}, profit factor {m['profit_factor']:.2f}, недель в плюс {m['weeks_positive']:.0%}",
        f"Max drawdown 90 дн {_p(m['mdd'])}, прибыльных месяцев {m['profitable_months']:.0f}/3, топ-3 сделки {_p(m['top3_share'], 0)} прибыли",
        f"{m['trades']:.0f} сделок за 90 дней, доходность 90 дн {_p(m['return_90d'])}, equity {fmt_usd(m['equity'])}",
        f"Копируемость на $50: {_p(e.copyability, 0)} от идеальной копии",
    ]


def _replicability(bt: WalletBacktest) -> list[str]:
    p = bt.profiles.get(bt.recommended or "conservative") or next(iter(bt.profiles.values()))
    r = p.replicability
    if r is None:
        return ["- нет данных (нет подходящих настроек)"]
    vain = "стопа нет" if r.stop_in_vain is None else f"{r.stop_in_vain:.0%} из {r.price_stops}"
    return [
        f"- Повторимых действий: {1 - r.lost_actions:.0%} (ордер < $10: {r.lost_actions:.0%}; пропущено по лимитам: {r.skipped_by_limits})",
        f"- Асимметрия: поздних входов {r.late_entries}, пропущенных частичных выходов {r.partial_exits_skipped}, "
        f"округлённых до $10 выходов {r.reduces_bumped}; влияние минимума $10 на PnL: {fmt_usd(r.min_size_pnl_impact)}",
        f"- Одновременные позиции p95 = {r.concurrency_p95:.1f} → нужно маржи {fmt_usd(r.margin_needed)} ({r.margin_share:.0%} депозита)",
        f"- Лестница входа: копия только первого входа {fmt_usd(r.ladder_pnl_first_only)} vs полная {fmt_usd(r.ladder_pnl_full)}",
        f"- Запас до ликвидации: моя дистанция {r.my_liq_distance:.1%} vs p95 MAE кошелька {_p(r.mae_p95)} → запас ×{_f(r.liq_cushion, '{:.1f}')}",
        f"- Стоп выбивал бы зря: {vain}",
        f"- Издержки копии (комиссии {fmt_usd(r.fees)}, проскальзывание и задержка {fmt_usd(r.slippage)}, funding {fmt_usd(r.funding)}): "
        f"{_p(r.cost_share, 0) if math.isfinite(r.cost_share) else 'валовая прибыль ≤ 0'} валовой прибыли",
    ]


def _details(run: ScoutRun, e: WalletEval, levels: list[float]) -> list[str]:
    bt = run.backtests.get(e.address)
    lines = [f"Страница: {explorer_url(e.address)}", ""]
    if e.linked:
        lines += [f"Связанные кошельки (считаются одним трейдером): {', '.join(e.linked)}", ""]
    lines += [f"**Стиль:** {_style(e)}", "", "**Ключевые метрики:**", ""]
    lines += [f"- {w}" for w in _why(e)]
    lines.append("")
    if bt is None:
        return [*lines, "_Walk-forward не запускался (вне топа)._", ""]
    lines += ["**Копируемость на $50:**", "", *_replicability(bt), ""]
    for name in PROFILES:
        lines += _profile_block(bt.profiles[name], levels)
    lines += [
        "**Walk-forward (средний профиль):**",
        "",
        "| Окно теста | PnL копии | Идеальная копия | Остановка |",
        "|---|---|---|---|",
    ]
    for t in bt.tests_for("medium"):
        stop = t.stop_reason or ("нет" if t.traded else (t.why or "не копировали"))
        lines.append(
            f"| {_d(t.fold.test0)} … {_d(t.fold.test1)} | {fmt_usd(t.pnl)} | {fmt_usd(t.ideal_pnl)} | {stop} |"
        )
    if not bt.tests:
        lines.append("| — | — | — | истории мало |")
    return [*lines, "", *(f"_{n}_" for n in bt.notes), ""]


def render(run: ScoutRun, top: int = 10) -> str:
    cfg, levels = run.cfg, list(run.cfg.goal.levels_usd)
    lines: list[str] = []
    now_s = datetime.fromtimestamp(run.now / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    recs = run.recommendable()
    lines += [
        "# hl_scout — отчёт по кандидатам",
        "",
        f"Дата: {now_s}. Депозит в copy-боте: {fmt_usd(cfg.deposit.total_usd)}. Задержка копирования: {cfg.copying.delay_s:g} с "
        f"(худший случай из {cfg.copying.delays_s}). Минимальный ордер: {fmt_usd(cfg.copying.min_order_usd)}.",
        "",
    ]
    if not run.copybot.verified:
        lines += [
            "> ⚠ **Семантика полей copy-бота не подтверждена** (`copybot_fields.yaml`): бэктест считает по рабочим "
            f"допущениям, комиссия бота принята {run.copybot.bot.fee_bps:g} б.п. на сделку. Сообщение «НАСТРОЙКИ» не "
            "выдаётся, пока нет документации бота.",
            "",
        ]
    lines += ["## Коротко", ""]
    lines.append(
        f"- Проверено кошельков: {run.funnel.get('проверено', 0)}, прошли все жёсткие фильтры: "
        f"{run.funnel.get('прошли все фильтры', 0)}, можно рекомендовать: {len(recs)}."
    )
    if recs:
        for e in recs:
            bt = run.backtests[e.address]
            p = bt.profiles[bt.recommended or "conservative"]
            lines.append(
                f"- **СЛЕДИТЬ-кандидат** `{e.address}` — профиль «{PROFILE_RU[bt.recommended or 'conservative']}», "
                f"риск {bt.risk}. 30 дней: {_mc_line(p.mc, levels)}"
            )
    else:
        lines.append(
            "- **Рекомендовать некого**: ни один кошелёк не прошёл все проверки (фильтры, навык, walk-forward, "
            "копируемость на $50). Это честный результат, а не ошибка."
        )
    best_p1000 = max(
        (
            bt.profiles[n].mc.p_ge.get(cfg.goal.target_usd, 0.0)
            for bt in run.backtests.values()
            for n in PROFILES
            if bt.profiles[n].mc
        ),
        default=0.0,
    )
    lines.append(
        f"- Ориентир {fmt_usd(cfg.goal.target_usd)} за {cfg.goal.horizon_days} дней: максимум по всем кандидатам и профилям "
        f"P = {_p(best_p1000, 2)}. Параметры под эту цель не подгонялись."
    )
    if run.process is not None:
        mc = run.process.mc.get("conservative")
        lines.append(
            f"- Процесс целиком (выбор кошелька на каждую дату только по прошлым данным), консервативный профиль: "
            f"{_mc_line(mc, levels)}"
        )
    lines.append("")

    lines += ["## Воронка отбора", "", "| Этап | Кошельков |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(run.funnel.items(), key=lambda kv: -kv[1])]
    lines.append("")

    lines += ["## Топ-10 по score", ""]
    lines.append(
        "| # | Кошелёк | Score | DSR | Sortino | PF | Недели+ | MDD | Сделок | Удерж. | Копир. | Окна+ | "
        "P(обнул.) | P(≥$100) | P(≥$1000) | Медиана 30д | Риск | Статус |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, e in enumerate(run.ranked[:top], 1):
        m = e.metrics
        bt = run.backtests.get(e.address)
        prof = bt.profiles[bt.recommended or "conservative"] if bt else None
        mc = prof.mc if prof else None
        status = "СЛЕДИТЬ-кандидат" if e in recs else _status(e, bt, cfg.recommend.min_dsr)
        lines.append(
            f"| {i} | [{short_address(e.address)}]({explorer_url(e.address)}) | {e.score:.0f} | {e.dsr:.2f} | {m['sortino']:.1f} | "
            f"{m['profit_factor']:.2f} | {m['weeks_positive']:.0%} | {_p(m['mdd'], 0)} | {m['trades']:.0f} | "
            f"{m['avg_hold_min'] / 60:.1f} ч | {_p(e.copyability, 0)} | {_p(prof.profitable_share, 0) if prof else '—'} | "
            f"{_p(mc.p_ruin) if mc else '—'} | {_p(mc.p_ge.get(100.0)) if mc else '—'} | {_p(mc.p_ge.get(1000.0), 2) if mc else '—'} | "
            f"{fmt_usd(mc.median) if mc else '—'} | {bt.risk if bt else '—'} | {status} |"
        )
    if not run.ranked:
        lines.append("| — | нет кошельков, прошедших жёсткие фильтры | | | | | | | | | | | | | | | | |")
    lines.append("")

    if run.forced:
        lines += ["## Разбор запрошенных адресов", ""]
        for e in run.forced:
            lines += [f"### `{e.address}`", "", "| Фильтр | Значение | Порог | |", "|---|---|---|---|"]
            lines += [f"| {f.name} | {f.value} | {f.threshold} | {'✅' if f.passed else '❌'} |" for f in e.filters]
            lines.append("")
            lines += _details(run, e, levels)
    for i, e in enumerate(run.ranked[:top], 1):
        lines += [f"### {i}. `{e.address}`", "", *_details(run, e, levels)]

    if run.split:
        s = run.split
        lines += [
            "## 1 кошелёк на $50 vs 2 кошелька по $25",
            "",
            f"Кошельки: `{s['a']}` и `{s['b']}`, профиль «{PROFILE_RU[s['profile']]}». Для каждой половины заново "
            "проверены минимум $10 и одновременные позиции.",
            "",
            f"- 1 × $50: {_mc_line(s.get('single'), levels)}",
            f"- 2 × $25: {_mc_line(s.get('split'), levels)}",
            "",
        ]
        for key in ("a_bt", "b_bt"):
            sub: WalletBacktest = s[key]
            p = sub.profiles[s["profile"]]
            if p.reject:
                lines.append(f"- На $25 у `{sub.address}` проблемы: {'; '.join(p.reject)}")
        lines.append("")

    if run.process is not None:
        pr = run.process
        lines += [
            "## Проверка всего процесса (walk-forward отбора)",
            "",
            "На каждую дату теста кошелёк выбирается только по данным до этой даты, затем копируется следующие "
            f"{cfg.backtest.test_days} дней. Это оценка того, что даст следование рекомендациям скаута.",
            "",
            "| Окно теста | Выбранный кошелёк | PnL (конс.) | PnL (средн.) | PnL (агр.) |",
            "|---|---|---|---|---|",
        ]
        for k, (fold, pick) in enumerate(pr.picks):
            pnl = [pr.outcomes[p][k].pnl if k < len(pr.outcomes[p]) else float("nan") for p in PROFILES]
            lines.append(
                f"| {_d(fold.test0)} … {_d(fold.test1)} | {short_address(pick) if pick else 'нет'} | "
                + " | ".join(fmt_usd(x) for x in pnl)
                + " |"
            )
        lines.append("")
        for p in PROFILES:
            lines.append(
                f"- «{PROFILE_RU[p]}»: {_mc_line(pr.mc.get(p), levels)}; прибыльных окон {_p(pr.profitable_share(p), 0)}"
            )
        lines.append("")

    lines += ["## Допущения и ограничения", ""]
    lines += [
        "- Семантика copy-бота — `copybot_fields.yaml` (пока не подтверждена документацией бота).",
        "- Цена моего входа = цена трейдера, сдвинутая движением рынка за время задержки (по самой мелкой свече: 1m за ~3.5 дня, "
        "15m за ~52 дня, 1h глубже) + проскальзывание по ликвидности; на старой истории добавлен штраф волатильности.",
        "- Ликвидация: поддерживающая маржа = половина начальной при макс. плече; при cross-ликвидации считаем, что теряется всё.",
        "- Пул кандидатов набран по объёму торгов, а не по прибыли; остаточный survivorship bias — правила попадания в "
        "неофициальный лидерборд неизвестны (см. docs/methodology.md).",
        "- Monte Carlo — блочный бутстрап дневных доходностей копии из тестовых окон; прошлое не гарантирует будущее.",
    ]
    return "\n".join(lines) + "\n"


def _status(e: WalletEval, bt: WalletBacktest | None, min_dsr: float) -> str:
    if bt is None:
        return "вне walk-forward"
    reasons = []
    if e.dsr < min_dsr:
        reasons.append(f"DSR {e.dsr:.2f} < {min_dsr:g} (не отличить от удачи)")
    if not bt.recommended:
        first = next((p for p in bt.profiles.values() if p.reject), None)
        reasons.append(first.reject[0] if first else "нет подходящего профиля")
    return "нет: " + "; ".join(reasons) if reasons else "в резерве"


def to_json(run: ScoutRun, top: int = 10) -> dict[str, Any]:
    def mc(m: McResult | None) -> dict[str, Any] | None:
        if m is None:
            return None
        return {
            "median": m.median,
            "p5": m.p5,
            "p95": m.p95,
            "p_ge": {str(k): v for k, v in m.p_ge.items()},
            "p_loss": m.p_loss,
            "p_ruin": m.p_ruin,
            "sample_days": m.sample_days,
        }

    out: dict[str, Any] = {"now": run.now, "funnel": run.funnel, "candidates": []}
    for e in run.ranked[:top]:
        bt = run.backtests.get(e.address)
        item: dict[str, Any] = {
            "address": e.address,
            "score": e.score,
            "dsr": e.dsr,
            "qvalue": e.qvalue,
            "metrics": {k: (v if math.isfinite(v) else None) for k, v in e.metrics.items()},
            "style": e.style,
            "copyability": e.copyability,
        }
        if bt:
            item["recommended_profile"] = bt.recommended
            item["risk"] = bt.risk
            item["profiles"] = {
                n: {
                    "settings": p.settings.__dict__ if p.settings else None,
                    "mc": mc(p.mc),
                    "reject": p.reject,
                    "windows": p.windows,
                    "profitable": p.profitable,
                }
                for n, p in bt.profiles.items()
            }
        out["candidates"].append(item)
    return out


def write(run: ScoutRun, reports_dir: str, top: int = 10) -> tuple[Path, Path]:
    d = Path(reports_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(run.now / 1000, tz=UTC).strftime("%Y%m%d_%H%M")
    md = d / f"top{top}_{stamp}.md"
    js = d / f"top{top}_{stamp}.json"
    md.write_text(render(run, top), encoding="utf-8")
    js.write_text(json.dumps(to_json(run, top), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return md, js
