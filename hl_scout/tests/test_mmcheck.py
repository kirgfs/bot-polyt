from __future__ import annotations

from hl_scout.config import Config
from hl_scout.mmcheck import assess, pick_trader, render, select_top_turnover
from hl_scout.util import DAY, HOUR, MIN

from helpers import T0

MM = "0x" + "1" * 40
WHALE = "0x" + "2" * 40
SUB = "0x" + "4" * 40


def _fill(t, px, sz, crossed, coin="BTC"):
    return {
        "time": t,
        "coin": coin,
        "side": "B",
        "px": str(px),
        "sz": str(sz),
        "startPosition": "0",
        "closedPnl": "0",
        "oid": t,
        "tid": t,
        "crossed": crossed,
        "fee": "0",
        "dir": "Open Long",
        "hash": "0x",
    }


def _row(addr, equity, m_pnl, m_vlm):
    return {
        "address": addr,
        "account_value": equity,
        "display_name": None,
        "perf": {"month": {"pnl": m_pnl, "roi": 0, "vlm": m_vlm}, "allTime": {"pnl": m_pnl, "roi": 0, "vlm": m_vlm}},
    }


def test_market_maker_is_not_copyable_for_three_reasons():
    cfg = Config()
    # a full page: 2000 fills in 2 hours (24 000/day), 95% maker, $5k each; $5M equity; 0.5 bp per $1 of turnover
    fills = [_fill(T0 - 2 * HOUR + i * 3600, 100_000, 0.05, crossed=i % 20 == 0) for i in range(2000)]
    r = assess(_row(MM, 5e6, 2e5, 4e9), MM, "сам адрес", 5e6, None, fills, cfg, T0)
    st = r.stats
    assert st is not None and st.fills_per_day > 20_000 and st.maker_share == 0.95
    assert r.median_copy_usd == 5_000 * 50 / 5e6  # $0.05: far below the $10 minimum
    assert st.below_min_share == 1.0 and st.bumped_turnover_day_usd > 200_000
    assert abs(r.edge_bps_month - 0.5) < 1e-9
    assert not r.copyable and len(r.reasons) == 3


def test_directional_whale_passes_the_economics():
    cfg = Config()
    fills = [_fill(T0 - 3 * DAY + i * 90 * MIN, 100_000, 5.0, crossed=True) for i in range(40)]  # $500k taker
    r = assess(_row(WHALE, 2e6, 1.5e6, 1e8), WHALE, "сам адрес", 2e6, None, fills, cfg, T0)
    st = r.stats
    assert st is not None and st.maker_share == 0.0 and st.fills_per_day < 100
    assert r.median_copy_usd == 12.5 and st.below_min_share == 0.0
    assert r.copyable
    assert "Не копируются: 0 из 1" in render([r], cfg, T0)


def test_no_fresh_fills_is_not_copyable_and_shows_no_zeros():
    cfg = Config()
    old = [_fill(T0 - 150 * DAY, 3000, 1, crossed=False)]  # the API gives only months-old fills
    r = assess(_row(MM, 6e7, 1e6, 4e10), SUB, "субаккаунт «F2»", 9e6, None, old, cfg, T0)
    assert r.stats is None and r.median_copy_usd is None and not r.copyable
    assert "API не отдаёт свежие сделки" in r.reasons[0]
    assert "нет свежих сделок в API" in render([r], cfg, T0)


def test_pick_trader_takes_the_most_active_sub_account():
    subs = [
        {"subAccountUser": "a", "clearinghouseState": {"marginSummary": {"accountValue": "9e6", "totalNtlPos": "0"}}},
        {"subAccountUser": "b", "clearinghouseState": {"marginSummary": {"accountValue": "1e6", "totalNtlPos": "5e6"}}},
    ]
    assert pick_trader(subs)["subAccountUser"] == "b"
    assert pick_trader([]) is None


def test_select_top_turnover():
    rows = [_row(MM, 1, 1, 5e9), _row(WHALE, 1, 1, 1e8), _row("0x" + "3" * 40, 1, 1, 7e9)]
    assert [r["address"] for r in select_top_turnover(rows, 2)] == ["0x" + "3" * 40, MM]
