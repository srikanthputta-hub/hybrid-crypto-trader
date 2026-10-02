"""
test_pack.py - verifies the strategies and the engine on synthetic data. Run: python test_pack.py
These tests prove the MECHANICS (no look-ahead, correct fills, correct accounting, costs bite).
They do not prove the strategies make money on real crypto - that needs real candles.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from engine import COST_PRESETS, Costs, EngineConfig, run_backtest, summarize
from strategies import MomParams, TrendParams, daily_regime, trend_ensemble, tsmom

SYMS = ["BTC", "ETH", "XRP", "SOL", "ADA", "DOGE", "AVAX", "LINK"]


# --------------------------------------------------------------------------- #
# Synthetic market generator (also used by run_report.py --synthetic)
# --------------------------------------------------------------------------- #
def make_market(n=900, seed=0, trendy=True, freq="1D", symbols=SYMS) -> dict[str, pd.DataFrame]:
    """Close-time indexed OHLCV for several correlated coins.
    trendy=True: persistent drift regimes (trend-following has a chance). trendy=False: pure random walk."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-02", periods=n, freq=freq, tz="UTC")
    state, mu = 1, 0.004
    common = np.empty(n)
    for i in range(n):
        if rng.random() > 0.98:
            state = -state
        common[i] = (state * mu if trendy else 0.0) + 0.020 * rng.standard_normal()
    out = {}
    for k, s in enumerate(symbols):
        beta = 1.0 if k == 0 else 0.6 + 0.8 * rng.random()
        ret = beta * common + (0.0 if k == 0 else 0.015) * rng.standard_normal(n)
        close = 100 * np.exp(np.cumsum(ret))
        open_ = np.r_[close[0], close[:-1]]
        hi = np.maximum(open_, close) * (1 + 0.01 * rng.random(n))
        lo = np.minimum(open_, close) * (1 - 0.01 * rng.random(n))
        out[s] = pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close, "volume": 1.0}, index=idx)
    return out


def build(market, strat="trend_ensemble", sma=60, tparams=TrendParams(), mparams=MomParams()):
    reg = daily_regime(market["BTC"], sma)
    fn = trend_ensemble if strat == "trend_ensemble" else tsmom
    p = tparams if strat == "trend_ensemble" else mparams
    frames = {s: fn(df, reg, p) for s, df in market.items()}
    start = max(f.index[f["warm"].to_numpy()][0] for f in frames.values())
    return {s: f.loc[start:] for s, f in frames.items()}


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_truncation_no_lookahead():
    mkt = make_market(seed=3)
    reg_full = daily_regime(mkt["BTC"], 60)
    for fn, p in ((trend_ensemble, TrendParams()), (tsmom, MomParams())):
        full = fn(mkt["ETH"], reg_full, p)
        for i in (300, 457, 600, 899):
            cut = mkt["ETH"].iloc[: i + 1]
            reg_cut = daily_regime(mkt["BTC"].iloc[: i + 1], 60)
            part = fn(cut, reg_cut, p)
            for col in ("atr", "enter", "hold", "rank", "regime"):
                a, b = full[col].iloc[i], part[col].iloc[i]
                same = (pd.isna(a) and pd.isna(b)) or bool(np.isclose(float(a), float(b)))
                assert same, f"look-ahead in {fn.__name__}.{col} at row {i}: {a} vs {b}"


def _scripted(opens, highs, lows, closes, enter_at, atr=2.0):
    idx = pd.date_range("2024-01-02", periods=len(opens), freq="1D", tz="UTC")
    df = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes}, index=idx)
    df["atr"] = atr
    df["enter"] = False
    df["hold"] = True
    df["rank"] = 1.0
    df.iloc[enter_at, df.columns.get_loc("enter")] = True
    return {"X": df}


def test_entry_fills_next_open_and_gap_stop_fills_at_open():
    # signal on bar 2 close -> entry at bar 3 open (100). stop = 100 - 3*2 = 94. Bar 5 gaps open at 90 -> fill 90.
    o = [100, 100, 100, 100, 100, 90, 90]
    h = [101, 101, 101, 101, 101, 91, 91]
    l = [99, 99, 99, 99, 99, 89, 89]
    c = [100, 100, 100, 100, 100, 90, 90]
    res = run_backtest(_scripted(o, h, l, c, enter_at=2), Costs(0, 0), EngineConfig(atr_mult=3.0, close_at_end=True))
    tr = res.trades
    assert len(tr) == 1 and tr.loc[0, "entry_px"] == 100 and tr.loc[0, "exit_px"] == 90
    assert tr.loc[0, "reason"] == "stop_gap"
    assert tr.loc[0, "exit_time"] > tr.loc[0, "entry_time"]


def test_intrabar_stop_fills_at_stop_price():
    o = [100, 100, 100, 100, 100, 100, 100]
    h = [101, 101, 101, 101, 101, 101, 101]
    l = [99, 99, 99, 99, 99, 93, 99]   # bar 5 trades down through the 94 stop
    c = [100, 100, 100, 100, 100, 96, 100]
    res = run_backtest(_scripted(o, h, l, c, enter_at=2), Costs(0, 0), EngineConfig(atr_mult=3.0))
    tr = res.trades
    assert tr.loc[0, "reason"] == "stop" and np.isclose(tr.loc[0, "exit_px"], 94.0)
    assert np.isclose(tr.loc[0, "R"], -1.0, atol=1e-9), "stop-out with zero costs must be exactly -1R"


def test_accounting_identity_and_costs():
    mkt = make_market(seed=5)
    frames = build(mkt)
    cfg = EngineConfig()
    r0 = run_backtest(frames, Costs(0, 0), cfg)
    r1 = run_backtest(frames, COST_PRESETS["kraken_T3_taker"], cfg)
    for r in (r0, r1):
        assert np.isclose(r.equity.iloc[-1], cfg.start_equity + r.trades["net_pnl"].sum(), rtol=1e-9), "P/L must reconcile"
        assert (r.equity > 0).all()
        assert (r.trades["expo_at_entry"] <= cfg.max_total_pct + 1e-9).all(), "total exposure cap breached at entry"
    assert r1.equity.iloc[-1] < r0.equity.iloc[-1], "costs must reduce final equity"
    assert r1.trades["fees"].sum() > 0 and r0.trades["fees"].sum() == 0


def test_per_position_cap():
    mkt = make_market(seed=2)
    frames = build(mkt)
    cfg = EngineConfig(max_pos_pct=0.10)
    r = run_backtest(frames, Costs(0, 0), cfg)
    notional = r.trades["qty"] * r.trades["entry_px"]
    assert (notional <= cfg.max_pos_pct * r.equity.max() * 1.0001).all()


def test_no_edge_random_walk_loses_costs_and_trend_market_can_win():
    # pure random walk: with zero costs expectancy ~ 0 R; with Kraken-like costs it must be clearly lower
    avg0, avgc = [], []
    for seed in range(12):
        fr = build(make_market(n=1200, seed=100 + seed, trendy=False))
        a = summarize(run_backtest(fr, Costs(0, 0), EngineConfig()))
        b = summarize(run_backtest(fr, COST_PRESETS["kraken_T3_taker"], EngineConfig()))
        if a["trades"] > 5 and b["trades"] > 5:
            avg0.append(a["avg_R"]); avgc.append(b["avg_R"])
    m0, mc = float(np.mean(avg0)), float(np.mean(avgc))
    print(f"   random walk: mean avg R zero-cost {m0:+.3f}, Kraken-T3-taker {mc:+.3f} (n={len(avg0)} seeds)")
    assert mc < m0 - 0.05, "costs should visibly reduce expectancy"
    assert mc < 0.05, "a no-edge market must not look profitable after realistic costs"
    # trendy market: expectancy should be clearly positive with zero costs
    avgt = []
    for seed in range(12):
        fr = build(make_market(n=1200, seed=200 + seed, trendy=True))
        a = summarize(run_backtest(fr, Costs(0, 0), EngineConfig()))
        if a["trades"] > 5:
            avgt.append(a["avg_R"])
    mt = float(np.mean(avgt))
    print(f"   trendy market: mean avg R zero-cost {mt:+.3f} (n={len(avgt)} seeds)")
    assert mt > m0 + 0.1, "trend strategy should do better in a market that actually trends"


def test_staggered_listing_does_not_truncate_window():
    from run_report import build_frames
    mkt = make_market(n=900, seed=4, symbols=["BTC", "ETH", "SOL"])
    mkt["SOL"] = mkt["SOL"].iloc[400:]            # SOL "lists" 400 bars later
    frames = build_frames(mkt, "trend_ensemble", 60)
    assert frames["BTC"].index[0] < frames["SOL"].index[0], "window must start with BTC, not the youngest coin"
    res = run_backtest(frames, COST_PRESETS["kraken_T3_taker"], EngineConfig())
    assert len(res.equity) == len(frames["BTC"]) and res.equity.iloc[-1] > 0


def test_bars_per_day_scaling_4h():
    p1, p6 = TrendParams(), TrendParams(bars_per_day=6)
    assert np.isclose(p6.stop_mult, p1.atr_mult * np.sqrt(6))
    mkt = make_market(n=2400, seed=9, freq="4h", symbols=["BTC", "ETH"])
    reg = daily_regime(mkt["BTC"], 60)
    full = trend_ensemble(mkt["ETH"], reg, p6)
    for i in (1500, 2000, 2399):
        part = trend_ensemble(mkt["ETH"].iloc[: i + 1], daily_regime(mkt["BTC"].iloc[: i + 1], 60), p6)
        for col in ("atr", "enter", "hold", "rank"):
            a, b = full[col].iloc[i], part[col].iloc[i]
            assert (pd.isna(a) and pd.isna(b)) or bool(np.isclose(float(a), float(b))), f"look-ahead at 4h {col} {i}"
    m = MomParams(bars_per_day=6)
    assert m.lookback * m.bars_per_day == 168 and m.rebalance_every * m.bars_per_day == 30


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print(f"{len(tests)} tests passed")
