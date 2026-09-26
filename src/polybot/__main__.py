"""Command line: `polybot <command>` (or `python -m polybot <command>`)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from polybot.core.config import (
    AppConfig,
    BaseConfig,
    MiniBotConfig,
    Settings,
    load_config,
    load_minibot_config,
)
from polybot.core.logging import configure_logging


def _load() -> tuple[Settings, AppConfig]:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format)
    return settings, load_config(settings.config_dir)


def _emit(text: str, out: str | None) -> None:
    print(text)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")


def cmd_record(args: argparse.Namespace) -> int:
    from polybot.recorder.app import run_recorder

    settings, cfg = _load()
    return asyncio.run(run_recorder(settings, cfg))


def cmd_geocheck(args: argparse.Namespace) -> int:
    from polybot.core.http import make_client
    from polybot.venues.polymarket.geoblock import check_geoblock

    _, cfg = _load()

    async def run() -> int:
        async with make_client(cfg.base.http) as http:
            status = await check_geoblock(http, cfg.base.geoblock)
        where = f"country={status.country} region={status.region}"
        print(f"{status.verdict.value}: {where} ({status.detail})")
        return 0 if status.allowed else 2

    return asyncio.run(run())


def cmd_discover(args: argparse.Namespace) -> int:
    from polybot.ops.discover import run_discover

    _, cfg = _load()
    _emit(asyncio.run(run_discover(cfg, args.max_pages)), args.out)
    return 0


def cmd_netcheck(args: argparse.Namespace) -> int:
    from polybot.ops.netcheck import render, run_netcheck, to_json

    _load()
    checks = asyncio.run(run_netcheck())
    _emit(to_json(checks) if args.json else render(checks), args.out)
    return 0 if all(c.verdict == "ok" for c in checks) else 1


def cmd_latency(args: argparse.Namespace) -> int:
    from polybot.ops.latency import measure, render

    _, cfg = _load()
    report = asyncio.run(
        measure(
            cfg,
            rest_samples=args.rest_samples,
            cold_samples=args.cold_samples,
            ws_seconds=args.ws_seconds,
            ws_ping_interval=args.ws_ping_interval,
            n_tokens=args.tokens,
        )
    )
    _emit(render(report), args.out)
    return 0


def cmd_oddspapi_eval(args: argparse.Namespace) -> int:
    from polybot.ops.oddspapi_eval import run_eval

    settings, cfg = _load()
    text = asyncio.run(
        run_eval(
            settings,
            cfg,
            args.step,
            bookmaker=args.bookmaker,
            max_calls=args.max_calls,
            per_group=args.per_group,
            n_fixtures=args.n_fixtures,
            duration_s=args.duration,
            interval_s=args.interval,
        )
    )
    _emit(text, args.out)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from polybot.analytics.m1_report import build_report

    settings, cfg = _load()
    _emit(build_report(settings, cfg, days=args.days), args.out)
    return 0


def cmd_daily(args: argparse.Namespace) -> int:
    from datetime import date

    from polybot.ops.daily import run_daily

    settings, cfg = _load()
    day = date.fromisoformat(args.date) if args.date else None
    result = run_daily(settings, cfg, day=day, delete=not args.no_delete)
    print(result.render())
    return 0 if result.ok else 1


def cmd_compact(args: argparse.Namespace) -> int:
    from polybot.data.sink import compact_day

    settings, _ = _load()
    removed = compact_day(settings.data_dir / "raw", args.date)
    print(f"{args.date}: merged and removed {removed} part files")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    from polybot.recorder.health import STATUS_FILE, check_status_file

    settings = Settings()
    path = settings.data_dir / "state" / (args.status or STATUS_FILE)
    ok, reason = check_status_file(path, args.max_age)
    print(reason)
    return 0 if ok else 1


def _load_minibot() -> tuple[Settings, BaseConfig, MiniBotConfig]:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format)
    base, cfg = load_minibot_config(settings.config_dir)
    return settings, base, cfg


def cmd_minibot(args: argparse.Namespace) -> int:
    from polybot.minibot.app import dry_run, run_minibot

    settings, base, cfg = _load_minibot()
    if args.dry_run:
        text, code = asyncio.run(dry_run(base, cfg, max_pages=args.max_pages))
        _emit(text, args.out)
        return code
    return asyncio.run(run_minibot(settings, base, cfg))


def cmd_minibot_status(args: argparse.Namespace) -> int:
    from polybot.core.http import make_client
    from polybot.minibot.app import make_notifier, read_status
    from polybot.minibot.engine import STATUS_FILE
    from polybot.minibot.report import Reporter
    from polybot.ops.telegram import NullNotifier, TelegramNotifier

    settings, base, cfg = _load_minibot()
    status = read_status(settings.data_dir / "state" / STATUS_FILE)
    if status is None:
        print("нет файла статуса мини-бота: он пишется, пока `polybot minibot` работает")
        return 1

    async def run() -> int:
        async with make_client(base.http) as http:
            notifier = make_notifier(settings, cfg, http) if args.send else NullNotifier()
            reporter = Reporter(notifier, cfg, settings.data_dir / "reports" / "minibot")
            text = reporter.status_text(status)
            print(text)
            if not args.send:
                return 0
            if not isinstance(notifier, TelegramNotifier):
                print("Telegram не настроен: TELEGRAM_BOT_TOKEN и TELEGRAM_ALLOWED_CHAT_IDS в .env")
                return 1
            await notifier.send(text)
            print(f"Telegram: отправлено {notifier.sent}, ошибок {notifier.failed}")
            return 0 if notifier.failed == 0 else 1

    return asyncio.run(run())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="polybot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("record", help="run the M1 recorder").set_defaults(func=cmd_record)
    sub.add_parser("geocheck", help="one geoblock check; exit 2 if not allowed").set_defaults(
        func=cmd_geocheck
    )

    p = sub.add_parser("discover", help="tags, sportsMarketType values, formats per sport")
    p.add_argument("--max-pages", type=int, default=20)
    p.add_argument("--out")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("netcheck", help="DNS/TCP/TLS/HTTP reachability, ISP block hints")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out")
    p.set_defaults(func=cmd_netcheck)

    p = sub.add_parser("latency", help="REST/WS latency to Polymarket (p50/p95)")
    p.add_argument("--rest-samples", type=int, default=200)
    p.add_argument("--cold-samples", type=int, default=20)
    p.add_argument("--ws-seconds", type=float, default=300.0)
    p.add_argument("--ws-ping-interval", type=float, default=2.0)
    p.add_argument("--tokens", type=int, default=20)
    p.add_argument("--out")
    p.set_defaults(func=cmd_latency)

    p = sub.add_parser("oddspapi-eval", help="OddsPapi free/trial checks (data_sources.md §6)")
    p.add_argument("step", choices=["meta", "fixtures", "coverage", "sample", "burst", "summary"])
    p.add_argument("--bookmaker", default="pinnacle")
    p.add_argument("--max-calls", type=int, default=10)
    p.add_argument("--per-group", type=int, default=8)
    p.add_argument("--n-fixtures", type=int, default=2)
    p.add_argument("--duration", type=float, default=180.0)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--out")
    p.set_defaults(func=cmd_oddspapi_eval)

    p = sub.add_parser("report", help="M1 data report from the recorded Parquet store")
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--out")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser(
        "daily",
        help="reports for finished UTC days (data/reports/daily), then delete old raw data",
    )
    p.add_argument("--date", help="YYYY-MM-DD: (re)build only this day's report")
    p.add_argument("--no-delete", action="store_true", help="keep all raw data")
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("compact", help="merge part files of a finished UTC day")
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.set_defaults(func=cmd_compact)

    p = sub.add_parser("health", help="Docker healthcheck: a status file is fresh")
    p.add_argument("--max-age", type=float, default=120.0)
    p.add_argument("--status", help="file in data/state (default: recorder_status.json)")
    p.set_defaults(func=cmd_health)
    _add_minibot_commands(sub)
    return parser


def _add_minibot_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("minibot", help="paper soccer mini-bot (decision 9); never real orders")
    p.add_argument("--dry-run", action="store_true", help="check leagues and selection, no quotes")
    p.add_argument("--max-pages", type=int, default=10, help="dry run: Gamma pages per tag")
    p.add_argument("--out")
    p.set_defaults(func=cmd_minibot)

    p = sub.add_parser("minibot-status", help="mini-bot status as in Telegram; --send sends it")
    p.add_argument("--send", action="store_true")
    p.set_defaults(func=cmd_minibot_status)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
