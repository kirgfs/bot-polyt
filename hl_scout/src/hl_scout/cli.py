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

from hl_scout import mmcheck
from hl_scout.accounts import ResolvedAddress, fetch_account, render_account, resolve_address
from hl_scout.config import Config, load_config, load_copybot
from hl_scout.discovery import Discovery
from hl_scout.hl.client import ConnectivityError, InfoClient
from hl_scout.hl.ws import WsSession
from hl_scout.log import get_logger, setup_logging
from hl_scout.store import Store
from hl_scout.util import DAY, HOUR, MIN, norm_address, now_ms, short_address

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

            def month_vlm(r: dict[str, Any]) -> float:
                return float(((dict(r.get("windowPerformances", [])).get("month") or {}).get("vlm")) or 0)

            rows.sort(key=month_vlm, reverse=True)
            # the very top rows are masters that trade through sub-accounts (api_notes §6): check fills on an
            # active row inside the discovery band instead
            band = [r for r in rows if month_vlm(r) <= cfg.discovery.pool_month_vlm_max_usd]
            addr = band[0]["ethAddress"] if band else None
            if rows:
                top = rows[0]["ethAddress"]
                subs = await client.sub_accounts(top)
                rec("subAccounts у вершины лидерборда", True, f"{top}: {len(subs)} субаккаунтов")
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
                rec(
                    "userFillsByTime",
                    bool(fr.fills),
                    f"{addr}: {len(fr.fills)} филлов, страниц {fr.pages}, усечено={fr.truncated}",
                )
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
        session = WsSession(
            cfg.api.ws_url,
            [{"type": "trades", "coin": "BTC"}],
            ping_every_s=cfg.api.ws_ping_s,
            pong_timeout_s=cfg.api.ws_pong_timeout_s,
        )
        loop = asyncio.get_running_loop()
        got = None
        async for msg in session.messages(loop.time() + 30):
            if msg.get("channel") == "trades" and msg.get("data"):
                got = msg["data"][0]
                break
        rec("WS trades: поле users", bool(got and "users" in got), f"пример: {got}")
        rec("WS: subscriptionResponse", session.all_acked, f"подтверждено {len(session.status.acked)} из 1")
    except Exception as exc:
        rec("WS trades", False, str(exc))
    return results


NETWORK_HINT = (
    "Проверьте интернет и файрвол: нужны api.hyperliquid.xyz (REST и WebSocket) и stats-data.hyperliquid.xyz. "
    "hl_scout работает только с публичным API и ограничения не обходит.\n"
    "Без сети можно пересобрать отчёт из кэша: python -m hl_scout report --skip-discovery"
)


async def preflight(cfg: Config, check_address: str | None = None) -> ResolvedAddress | None:
    """Fail fast before any long job: one cheap request, then (for /check-like commands) resolve the address."""
    async with InfoClient(cfg.api) as client:
        n = await client.preflight()
        log.info("preflight_ok", coins_priced=n)
        if check_address is None:
            return None
        resolved = await resolve_address(client, check_address)
        if resolved.note:
            print(f"ⓘ {resolved.note}")
        return resolved


async def show_account(cfg: Config, address: str) -> int:
    async with InfoClient(cfg.api) as client:
        resolved = await resolve_address(client, address)
        snap = await fetch_account(client, resolved.address)
    print(render_account(snap, resolved))
    return 0


async def check_market_makers(cfg: Config, count: int | None, copybot_path: str | None = None) -> int:
    """`mm`: the biggest rows of the leaderboard by turnover — can a copy on my deposit repeat them? (mmcheck.py)"""
    bot = load_copybot(copybot_path)
    min_order = max(cfg.copying.min_order_usd, bot.semantics.min_copy_usd)
    store = Store(cfg.storage.sqlite_path)
    try:
        async with InfoClient(cfg.api) as client:
            disc = Discovery(cfg, store, client)
            await disc.refresh_leaderboard()
            top = mmcheck.select_top_turnover(store.leaderboard_rows(), count or cfg.mm_check.count)
            now = now_ms()
            rows = []
            for r in top:
                addr = r["address"]
                trader, note, equity = addr, "сам адрес", float(r.get("account_value") or 0.0)
                pf = await disc.portfolio(addr)
                if disc._trades_through_subaccounts(addr, pf):
                    subs = await client.sub_accounts(addr)
                    best = mmcheck.pick_trader(subs)
                    if best is not None:
                        trader = str(best["subAccountUser"]).lower()
                        ms = (best.get("clearinghouseState") or {}).get("marginSummary") or {}
                        equity = float(ms.get("accountValue") or 0.0)
                        note = f"субаккаунт «{best.get('name')}» `{short_address(trader)}` (из {len(subs)})"
                        pf = await disc.portfolio(trader)
                fills = await client.user_fills(trader)
                rows.append(
                    mmcheck.assess(
                        r, trader, note, equity, pf, fills, cfg, now, min_order=min_order, bot_fee_bps=bot.bot.fee_bps
                    )
                )
                log.info("mm_checked", address=addr, trader=trader, copyable=rows[-1].copyable)
    finally:
        store.close()
    out = Path(cfg.storage.reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    md = out / "mm_check.md"
    md.write_text(mmcheck.render(rows, cfg, now, min_order, bot.bot.name or ""), encoding="utf-8")
    (out / "mm_check.json").write_text(
        json.dumps(mmcheck.to_json(rows), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    for row in rows:
        verdict = "экономика копии не против, нужен полный бэктест" if row.copyable else "не копируется"
        print(f"{row.address} ({row.trader_note}): {verdict}; прибыль на $1 оборота {row.edge_bps_month:.2f} б.п.")
    print(f"Отчёт: {md}")
    return 0


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
    p_acc = sub.add_parser("account", help="мой copy-аккаунт по публичному адресу: баланс, позиции, API-кошельки")
    p_acc.add_argument("address", nargs="?", default=None, help="по умолчанию project.my_copy_account из config.yaml")
    p_mm = sub.add_parser("mm", help="маркет-мейкеры сверху лидерборда: можно ли их копировать на мой депозит")
    p_mm.add_argument("--count", type=int, default=None, help="сколько аккаунтов (по умолчанию mm_check.count)")
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
    p_rep.add_argument(
        "--address", action="append", default=[], help="разобрать этот адрес полностью (можно несколько раз)"
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg.logging.level, cfg.logging.file)
    try:
        return _run(args, cfg)
    except ConnectivityError as exc:
        print(f"❌ Нет связи с Hyperliquid API: {exc}\n{NETWORK_HINT}")
        return 2
    except ValueError as exc:  # AddressError and malformed addresses
        print(f"❌ {exc}")
        return 3


def _run(args: argparse.Namespace, cfg: Config) -> int:
    if args.cmd == "selfcheck":
        asyncio.run(preflight(cfg))
        res = asyncio.run(selfcheck(cfg))
        Path("logs").mkdir(exist_ok=True)
        Path("logs/selfcheck.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0 if all(r["ok"] for r in res) else 1
    if args.cmd == "account":
        address = args.address or cfg.project.my_copy_account
        if not address:
            print(
                "Укажите адрес: python -m hl_scout account 0x… или project.my_copy_account в config.yaml "
                "(публичный адрес, ключ не нужен)"
            )
            return 3
        address = norm_address(address)
        asyncio.run(preflight(cfg))
        return asyncio.run(show_account(cfg, address))
    if args.cmd == "mm":
        asyncio.run(preflight(cfg))
        return asyncio.run(check_market_makers(cfg, args.count, args.copybot))
    if args.cmd == "check":
        args.address = norm_address(args.address)  # a typo must not cost a network round trip
    online = args.cmd == "discover" or not getattr(args, "skip_discovery", False)
    extra: list[str] = []
    if args.cmd == "check":
        if online:
            resolved = asyncio.run(preflight(cfg, args.address))
            extra = [resolved.address] if resolved else []
        else:
            extra = [norm_address(args.address)]
    elif online:
        asyncio.run(preflight(cfg))
    if args.cmd == "report":
        extra = [norm_address(a) for a in args.address]
    store = Store(cfg.storage.sqlite_path)
    try:
        if online:
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
