"""Command line: python -m hl_scout <command>. Works on Windows (no uvloop, UTF-8 console output)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from hl_scout.config import Config, load_config, load_copybot
from hl_scout.discovery import Discovery
from hl_scout.hl.client import InfoClient
from hl_scout.hl.ws import WsSession
from hl_scout.log import get_logger, setup_logging
from hl_scout.store import Store
from hl_scout.util import DAY, HOUR, MIN, norm_address, now_ms

log = get_logger(__name__)


def _console_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]


async def discover(
    cfg: Config, store: Store, *, listen_min: float | None, use_ws: bool, use_lb: bool, extra: list[str] | None = None
) -> list[str]:
    async with InfoClient(cfg.api) as client:
        return await Discovery(cfg, store, client).run(listen_min=listen_min, use_ws=use_ws, use_lb=use_lb, extra=extra)


async def selfcheck(cfg: Config) -> list[dict[str, Any]]:
    """Checks the facts marked [DOC-S] in docs/api_notes.md §9 against the live API."""
    results: list[dict[str, Any]] = []

    def rec(name: str, ok: bool, detail: str) -> None:
        results.append({"check": name, "ok": ok, "detail": detail})
        print(f"{'✓' if ok else '✗'} {name}: {detail}")

    now = now_ms()
    async with InfoClient(cfg.api) as client:
        try:
            meta = await client.meta_and_asset_ctxs()
            uni = meta[0]["universe"]
            rec(
                "metaAndAssetCtxs",
                "szDecimals" in uni[0] and "maxLeverage" in uni[0],
                f"{len(uni)} перпов, пример {uni[0]}",
            )
        except Exception as exc:
            rec("metaAndAssetCtxs", False, str(exc))
            return results
        addr = None
        try:
            raw = await client.get_json(cfg.discovery.leaderboard_url)
            rows = raw.get("leaderboardRows", []) if isinstance(raw, dict) else []
            windows = [w[0] for w in rows[0].get("windowPerformances", [])] if rows else []
            rec("лидерборд", bool(rows), f"{len(rows)} строк, окна {windows}")
            rows.sort(
                key=lambda r: -float(((dict(r.get("windowPerformances", [])).get("month") or {}).get("vlm")) or 0)
            )
            addr = rows[0]["ethAddress"] if rows else None
        except Exception as exc:
            rec("лидерборд", False, str(exc))
        try:
            c = await client.candles("BTC", "1m", now - 2 * HOUR, now)
            rec("candleSnapshot 1m", bool(c) and {"t", "T", "o", "h", "l", "c"} <= set(c[0]), f"{len(c)} свечей")
            deep = await client.candles("BTC", "1m", now - 10 * DAY, now)
            span = (int(deep[-1]["t"]) - int(deep[0]["t"])) / DAY if deep else 0
            rec("глубина 1m-свечей", span < 4, f"{len(deep)} свечей ≈ {span:.1f} дн (ожидаем ≈3.5 дн)")
        except Exception as exc:
            rec("candleSnapshot", False, str(exc))
        try:
            f = await client.funding_history("BTC", now - DAY, now)
            med = statistics.median(abs(float(x["fundingRate"])) for x in f) if f else float("nan")
            rec(
                "fundingHistory часовая ставка",
                bool(f) and len(f) <= 25,
                f"{len(f)} записей за сутки, |медиана| {med:.8f}",
            )
        except Exception as exc:
            rec("fundingHistory", False, str(exc))
        if addr:
            try:
                fr = await client.user_fills_by_time(addr, now - 7 * DAY, now)
                times = [int(x["time"]) for x in fr.fills]
                rec("userFillsByTime", True, f"{len(fr.fills)} филлов, страниц {fr.pages}, усечено={fr.truncated}")
                page = await client.info(
                    {
                        "type": "userFillsByTime",
                        "user": addr,
                        "startTime": now - 7 * DAY,
                        "endTime": now,
                        "aggregateByTime": True,
                    }
                )
                asc = all(int(a["time"]) <= int(b["time"]) for a, b in itertools.pairwise(page))
                rec("порядок филлов по возрастанию", asc, f"первая страница {len(page)} шт.")
                liq = [x for x in fr.fills if x.get("liquidation")]
                rec(
                    "поле liquidation в филлах",
                    True,
                    f"{len(liq)} филлов с liquidation за 7 дн" + (f", пример {liq[0]['liquidation']}" if liq else ""),
                )
                del times
            except Exception as exc:
                rec("userFillsByTime", False, str(exc))
            try:
                pf = await client.portfolio(addr)
                info = []
                for name, data in pf:
                    pts = [int(p[0]) for p in data.get("accountValueHistory", [])]
                    step = statistics.median(b - a for a, b in itertools.pairwise(pts)) / MIN if len(pts) > 2 else 0
                    info.append(f"{name}: {len(pts)} точек, шаг ≈ {step:.0f} мин")
                rec("portfolio: гранулярность", True, "; ".join(info))
            except Exception as exc:
                rec("portfolio", False, str(exc))
            try:
                role = await client.user_role(addr)
                rec("userRole", "role" in role, json.dumps(role))
            except Exception as exc:
                rec("userRole", False, str(exc))
    try:
        session = WsSession(cfg.api.ws_url, [{"type": "trades", "coin": "BTC"}])
        loop = asyncio.get_running_loop()
        got = None
        async for msg in session.messages(loop.time() + 30):
            if msg.get("channel") == "trades" and msg.get("data"):
                got = msg["data"][0]
                break
        rec("WS trades: поле users", bool(got and "users" in got), f"пример: {got}")
    except Exception as exc:
        rec("WS trades", False, str(exc))
    return results


def main(argv: list[str] | None = None) -> int:
    _console_utf8()
    parser = argparse.ArgumentParser(prog="hl_scout", description="Скаут кошельков Hyperliquid для copy-бота")
    parser.add_argument("--config", default=None, help="путь к config.yaml")
    parser.add_argument("--copybot", default=None, help="путь к copybot_fields.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selfcheck", help="сверить факты об API на живом API (docs/api_notes.md §9)")
    p_disc = sub.add_parser("discover", help="собрать кандидатов и данные в SQLite")
    p_rep = sub.add_parser("report", help="discovery → фильтры → score → бэктест → отчёт по топ-N")
    p_chk = sub.add_parser("check", help="полный разбор и бэктест одного адреса")
    p_chk.add_argument("address")
    for p in (p_disc, p_rep, p_chk):
        p.add_argument("--listen-min", type=float, default=None, help="сколько минут слушать крупные сделки (WS)")
        p.add_argument("--no-ws", action="store_true", help="не слушать WS крупных сделок")
        p.add_argument("--no-leaderboard", action="store_true")
    for p in (p_rep, p_chk):
        p.add_argument("--skip-discovery", action="store_true", help="только кэш, без сети")
        p.add_argument("--no-process", action="store_true", help="без walk-forward всего процесса (быстрее)")
        p.add_argument(
            "--delay", type=float, default=None, help="задержка copy-бота, с (по умолчанию худшая из конфига)"
        )
    p_rep.add_argument("--top", type=int, default=10)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg.logging.level, cfg.logging.file)
    store = Store(cfg.storage.sqlite_path)
    try:
        if args.cmd == "selfcheck":
            res = asyncio.run(selfcheck(cfg))
            Path("logs").mkdir(exist_ok=True)
            Path("logs/selfcheck.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
            return 0 if all(r["ok"] for r in res) else 1
        extra: list[str] = []
        if args.cmd == "check":
            extra = [norm_address(args.address)]
        if args.cmd == "discover" or not getattr(args, "skip_discovery", False):
            asyncio.run(
                discover(
                    cfg,
                    store,
                    listen_min=args.listen_min,
                    use_ws=not args.no_ws and args.cmd != "check",
                    use_lb=not args.no_leaderboard,
                    extra=extra,
                )
            )
        if args.cmd == "discover":
            return 0
        from hl_scout import report
        from hl_scout.pipeline import analyze

        copybot = load_copybot(args.copybot)
        top = getattr(args, "top", 10)
        run = analyze(
            cfg, store, copybot, now_ms(), top_n=top, process=not args.no_process, delay_s=args.delay, force=extra
        )
        md, js = report.write(run, cfg.storage.reports_dir, top=top)
        print(report.render(run, top))
        print(f"\nОтчёт сохранён: {md}\nJSON: {js}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
