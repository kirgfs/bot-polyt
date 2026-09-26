"""Discovery → SQLite → analysis against a fake Info API that answers in the documented shapes (api_notes §2)."""

from __future__ import annotations

import json

import httpx
import numpy as np

from hl_scout.config import (
    ApiCfg,
    BacktestCfg,
    Config,
    CopyBotSpec,
    CopySemantics,
    DiscoveryCfg,
    GridCfg,
    LargeTradesCfg,
)
from hl_scout.discovery import Discovery, deep_addresses, load_wallet, parse_leaderboard, select_pool
from hl_scout.hl.client import INTERVAL_MS, InfoClient
from hl_scout.pipeline import analyze
from hl_scout.store import Store
from hl_scout.util import HOUR, MIN, now_ms

from synth import TraderSpec, make_wallet, make_world


def fake_api(world, wallets, masters=None, hidden=()):
    """`masters`: leaderboard-only master address → its sub-account wallets; `hidden`: wallets that exist on the
    API but have no leaderboard row of their own (sub-accounts)."""
    masters = masters or {}
    by_addr = {w.address: w for w in [*wallets, *hidden]}
    ft = np.arange(world.t_start + HOUR, world.t_end, HOUR, dtype=np.int64)

    def leaderboard_rows():
        rows = [
            {
                "ethAddress": w.address,
                "accountValue": "40000",
                "windowPerformances": [
                    ["day", {"pnl": "1", "roi": "0.001", "vlm": "1000"}],
                    ["week", {"pnl": "1", "roi": "0.001", "vlm": "100000"}],
                    ["month", {"pnl": "1", "roi": "0.001", "vlm": "5000000"}],
                    ["allTime", {"pnl": "1", "roi": "0.01", "vlm": "90000000"}],
                ],
                "prize": 0,
                "displayName": None,
            }
            for w in wallets
        ]
        rows += [
            {
                "ethAddress": m,
                "accountValue": "40000",
                "windowPerformances": [
                    ["month", {"pnl": "1", "roi": "0.001", "vlm": "5000000"}],
                    ["allTime", {"pnl": "1", "roi": "0.01", "vlm": "90000000"}],
                ],
            }
            for m in masters
        ]
        rows.append(
            {"ethAddress": "0x" + "9" * 40, "accountValue": "10", "windowPerformances": [["month", {"vlm": "1"}]]}
        )
        return {"leaderboardRows": rows}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=leaderboard_rows())
        body = json.loads(request.content)
        t = body["type"]
        user = body.get("user")
        w = by_addr.get(user) if user else None
        if t == "metaAndAssetCtxs":
            return httpx.Response(200, json=world.meta_json)
        if t == "spotMetaAndAssetCtxs":
            return httpx.Response(200, json=[{"universe": [], "tokens": []}, []])
        if t == "portfolio":
            return httpx.Response(200, json=w.portfolio if w else [])
        if t == "userFillsByTime":
            rows = [f for f in (w.raw_fills if w else []) if body["startTime"] <= f["time"] <= body["endTime"]]
            return httpx.Response(200, json=rows[:2000])
        if t == "userNonFundingLedgerUpdates":
            return httpx.Response(
                200, json=[u for u in (w.ledger if w else []) if body["startTime"] <= u["time"]][:500]
            )
        if t == "clearinghouseState":
            return httpx.Response(200, json=w.clearinghouse if w else {})
        if t == "spotClearinghouseState":
            return httpx.Response(200, json=w.spot_state if w else {"balances": []})
        if t == "userRole":
            return httpx.Response(200, json={"role": "user"})
        if t == "subAccounts":
            subs = [
                {
                    "name": f"S{i}",
                    "subAccountUser": s.address,
                    "master": user,
                    "clearinghouseState": {"marginSummary": {"accountValue": "40000"}},
                }
                for i, s in enumerate(masters.get(user, []))
            ]
            return httpx.Response(200, json=subs)
        if t == "candleSnapshot":
            req = body["req"]
            step = INTERVAL_MS[req["interval"]]
            rows = world.candles[req["coin"]][req["interval"]]
            out = [
                {
                    "t": r[0],
                    "T": r[0] + step - 1,
                    "s": req["coin"],
                    "i": req["interval"],
                    "o": str(r[1]),
                    "h": str(r[2]),
                    "l": str(r[3]),
                    "c": str(r[4]),
                    "v": "1",
                    "n": 1,
                }
                for r in rows
                if req["startTime"] <= r[0] <= req["endTime"]
            ]
            return httpx.Response(200, json=out[:5000])
        if t == "fundingHistory":
            sel = ft[(ft >= body["startTime"]) & (ft <= body["endTime"])][:500]
            return httpx.Response(
                200,
                json=[{"coin": body["coin"], "fundingRate": "0.0000125", "premium": "0", "time": int(x)} for x in sel],
            )
        return httpx.Response(422, text=f"unknown {t}")

    return handler


async def test_discovery_fills_the_cache_and_analysis_reads_it(tmp_path):
    now = now_ms() - now_ms() % MIN
    world = make_world(seed=3, days=130, t_end=now)
    good = make_wallet(
        world,
        TraderSpec(
            "0x" + "1" * 40, skill=0.7, trades_per_day=1.0, hold_min=(240, 2000), notional=15_000, equity=40_000
        ),
        seed=1,
    )
    scalper = make_wallet(
        world,
        TraderSpec("0x" + "2" * 40, skill=0.6, trades_per_day=15, hold_min=(1, 6), notional=5_000, equity=20_000),
        seed=2,
    )
    hidden = make_wallet(
        world,
        TraderSpec(
            "0x" + "3" * 40, skill=0.7, trades_per_day=1.0, hold_min=(240, 2000), notional=15_000, equity=40_000
        ),
        seed=4,
    )
    master = "0x" + "7" * 40  # its leaderboard row sums the hidden sub-account; the master itself does not trade
    grid = GridCfg(target_position_usd=[15, 30], leverage=[1, 3], buy_times=[0], small_size=["skip"], price_sl=["none"])
    cfg = Config(
        discovery=DiscoveryCfg(history_days=120, large_trades=LargeTradesCfg(enabled=False)),
        backtest=BacktestCfg(grid=grid, tune_mc_paths=200),
        api=ApiCfg(backoff_base_s=0.0, weight_budget_per_min=10**9),
    )
    store = Store(tmp_path / "cache.sqlite")
    api = fake_api(world, [good, scalper], masters={master: [hidden]}, hidden=[hidden])
    client = InfoClient(cfg.api, transport=httpx.MockTransport(api))
    done = await Discovery(cfg, store, client).run(listen_min=0, use_ws=False, use_lb=True)
    await client.aclose()
    assert good.address in done
    assert "0x" + "9" * 40 not in done  # the inactive dust account is not even in the pool
    assert hidden.address in done and master not in done  # the sub-account is found through its master
    assert {"subaccount", "control"} <= store.addresses()[hidden.address]  # inherits the master's sample
    assert deep_addresses(store) == sorted(done)
    loaded = load_wallet(store, good.address)
    since = now - 120 * 86_400_000
    assert len(loaded.raw_fills) == len([f for f in good.raw_fills if f["time"] >= since])
    assert store.candles_get("BTC", "1m") and store.funding_get("BTC")
    run = analyze(cfg, store, CopyBotSpec(), now, top_n=2, process=False)
    by = {e.address: e for e in run.evals}
    assert scalper.address not in by or not by[scalper.address].eligible  # dropped at stage 1 or by the filters
    assert by[good.address].metrics["trades"] > 0
    # the same analysis with the copy bot's real sizing rule (ApexLiquid: scaled by both balances)
    apex = CopyBotSpec(semantics=CopySemantics(ratio_applies_to="balance_scaled"))
    run2 = analyze(cfg, store, apex, now, top_n=2, process=False, only=[good.address])
    bt = run2.backtests.get(good.address)
    assert bt is not None and any(t.settings is not None for t in bt.tests)  # settings found within ratio 0.01–10
    store.close()


def test_control_sample_ignores_profit():
    cfg = Config()

    def row(addr, month_vlm, pnl):
        return {
            "address": addr,
            "account_value": 5000,
            "perf": {
                "month": {"vlm": month_vlm, "pnl": pnl, "roi": pnl / 5000},
                "allTime": {"vlm": month_vlm, "pnl": pnl, "roi": pnl / 5000},
            },
        }

    rows = [row("0xloser", 5e6, -4e3), row("0xwinner", 1e6, 4e3), row("0xidle", 10, 1e3)]
    pool, control = select_pool(rows, cfg, extra={"0xwhale": 1e6}, manual=["0xmine"])
    assert pool[0] == "0xmine" and pool[-1] == "0xwhale"
    assert control == {"0xloser", "0xwinner"}  # the loser is sampled too: no selection on results
    assert "0xidle" not in pool  # below the activity band


def test_parse_leaderboard_tolerates_odd_rows():
    raw = {
        "leaderboardRows": [
            {
                "ethAddress": "0x" + "a" * 40,
                "accountValue": "12.5",
                "windowPerformances": [["month", {"pnl": "1", "vlm": "x"}]],
            },
            {"ethAddress": "not-an-address"},
            "garbage",
        ]
    }
    rows = parse_leaderboard(raw)
    assert len(rows) == 1 and rows[0]["perf"]["month"]["vlm"] == 0.0 and rows[0]["account_value"] == 12.5


def test_parse_apex_top_real_shape():
    from pathlib import Path

    from hl_scout.discovery import parse_apex_top

    raw = json.loads((Path(__file__).parent / "fixtures" / "apex_top_trades.json").read_text(encoding="utf-8"))
    rows = parse_apex_top(raw)
    by = {r["address"]: r for r in rows}
    assert "0xc1a4ecaa0889dd50e839bbea44d2884f7bb0ea31" in by  # the trader from the user's screenshot
    r = by["0xc1a4ecaa0889dd50e839bbea44d2884f7bb0ea31"]
    assert r["perpsBalance"] > 0 and r["maxDrawdown"] is not None
    assert parse_apex_top({"code": 1}) == [] and parse_apex_top([]) == []
