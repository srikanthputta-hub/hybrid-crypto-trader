"""v2_signals.py - starter building blocks for the v2 trend-following design.

What this is: closed-candle data fetch (CCXT), indicators (this repo's own
src.indicators, not TA-Lib -- see note below), a Donchian breakout ensemble,
a BTC regime filter, and ATR-based position sizing.
What this is NOT: a finished bot, a backtester, or proof of any edge. Every
parameter is a starting value to TEST (see docs/crypto_bot_upgrade_playbook.md,
Section 4 and 6). Nothing here is wired into the live bot (src/) yet.

Adapted from the playbook's original TA-Lib version: this repo's
src/indicators.py already implements SMA (plain rolling mean) and ATR
(Wilder's smoothing, same convention TA-Lib's ATR uses by default) in pure
pandas -- see that module's own docstring, "no extra TA dependency needed".
Swapping talib.SMA/talib.ATR for src.indicators.sma/atr keeps this numerically
equivalent without adding a new C-library dependency to the deploy (the VM
this bot runs on doesn't have TA-Lib installed, and the existing codebase
deliberately avoids needing it).

Conventions that prevent look-ahead:
  * Bars are indexed by CLOSE time (open time + timeframe). A row only contains
    information that was known at that close.
  * A signal computed on row i is executed at the NEXT bar's open (row i+1).
  * The regime filter uses the latest COMPLETED daily BTC bar at each close time.
  * The still-forming candle is dropped when fetching.

Run the self-tests:   python research/v2_signals.py
Requires:             pip install ccxt pandas numpy (already in requirements.txt)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.indicators import atr as _atr_series
from src.indicators import sma as _sma_series


@dataclass(frozen=True)
class Params:
    lookbacks: tuple = (20, 40, 60, 90, 150)  # test grid, not proven values
    exit_divisor: int = 2          # exit channel = lookback // exit_divisor bars
    atr_len: int = 14
    atr_mult: float = 3.0          # stop distance = atr_mult * ATR
    regime_sma: int = 200          # on daily BTC closes
    risk_pct: float = 0.0075       # equity risked per full-size trade
    max_notional_pct: float = 0.20  # cap on any single position's notional


# --------------------------------------------------------------------------- #
# Data (CCXT)
# --------------------------------------------------------------------------- #
def fetch_closed_ohlcv(exchange, symbol: str, timeframe: str, limit: int = 1000) -> pd.DataFrame:
    """Fetch OHLCV and drop the still-forming candle. Index = bar CLOSE time (UTC).

    `exchange` is a ccxt exchange instance created with enableRateLimit=True.
    For perps use unified symbols like 'BTC/USDT:USDT'. Paginate with `since`
    if you need more history than one call returns.
    """
    raw = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    tf_ms = int(exchange.parse_timeframe(timeframe) * 1000)
    df = pd.DataFrame(raw, columns=["open_ms", "open", "high", "low", "close", "volume"])
    df["close_ms"] = df["open_ms"] + tf_ms
    df = df[df["close_ms"] <= exchange.milliseconds()]  # keep only completed bars
    df.index = pd.to_datetime(df["close_ms"], unit="ms", utc=True)
    df = df.drop(columns=["open_ms", "close_ms"]).astype(float)
    return df[~df.index.duplicated(keep="last")].sort_index()


# --------------------------------------------------------------------------- #
# Indicators and signals
# --------------------------------------------------------------------------- #
def regime_on(btc_daily: pd.DataFrame, target_index: pd.DatetimeIndex, sma_len: int = 200) -> pd.Series:
    """True when the latest COMPLETED daily BTC close is above its SMA.

    `btc_daily` must be indexed by close time (use fetch_closed_ohlcv with '1d').
    """
    sma = _sma_series(btc_daily["close"], sma_len)
    flag = (btc_daily["close"] > sma) & sma.notna()
    return flag.reindex(target_index, method="ffill").fillna(False).astype(bool)


def _donchian_state(close: np.ndarray, entry_level: np.ndarray, exit_level: np.ndarray) -> np.ndarray:
    """1 while 'long': enter on close above prior N-bar high, exit on close below prior exit-low."""
    state = np.zeros(len(close), dtype=np.int8)
    on = 0
    for i in range(len(close)):
        if on == 0:
            if close[i] > entry_level[i]:  # NaN compares False, so warm-up is safe
                on = 1
        elif close[i] < exit_level[i]:
            on = 0
        state[i] = on
    return state


def build_signals(df: pd.DataFrame, regime: pd.Series, p: Params = Params()) -> pd.DataFrame:
    """Add atr, votes, target_frac. Row i uses only data up to and including row i."""
    out = df.copy()
    c = out["close"].to_numpy(float)
    out["atr"] = _atr_series(out, p.atr_len)

    votes = np.zeros(len(out), dtype=np.int16)
    for n in p.lookbacks:
        entry = out["high"].rolling(n).max().shift(1).to_numpy()   # prior n bars, excludes current
        exit_ = out["low"].rolling(max(2, n // p.exit_divisor)).min().shift(1).to_numpy()
        votes += _donchian_state(c, entry, exit_)
    out["votes"] = votes
    out["regime_on"] = regime.reindex(out.index).fillna(False).astype(bool)
    # fraction of full size to hold AFTER this bar closes (act on it at the next open)
    out["target_frac"] = np.where(out["regime_on"], out["votes"] / len(p.lookbacks), 0.0)
    return out


def trailing_stop(close: pd.Series, atr: pd.Series, entry_pos: int, mult: float) -> pd.Series:
    """Chandelier stop: highest close since entry minus mult*ATR. Never moves down."""
    hh = close.iloc[entry_pos:].cummax()
    stop = hh - mult * atr.iloc[entry_pos:]
    return stop.cummax()


# --------------------------------------------------------------------------- #
# Sizing and costs
# --------------------------------------------------------------------------- #
def position_qty(equity: float, price: float, atr: float, frac: float, p: Params = Params()) -> float:
    """Quantity so that hitting the ATR stop loses ~risk_pct of equity at full size.
    Scaled by `frac` (ensemble vote) and capped by max_notional_pct."""
    if not (equity > 0 and price > 0 and atr > 0) or frac <= 0:
        return 0.0
    stop_dist = p.atr_mult * atr
    qty = equity * p.risk_pct / stop_dist
    qty = min(qty, equity * p.max_notional_pct / price)
    return qty * frac


def round_trip_cost_pct(fee_pct: float, slippage_pct: float) -> float:
    """Percent of notional lost to costs for one entry plus one exit (both sides pay)."""
    return 2.0 * (fee_pct + slippage_pct)


def cost_as_fraction_of_risk(rt_cost_pct: float, stop_dist_pct: float) -> float:
    """How much of your 1R is eaten by costs before price moves. Keep this small."""
    return rt_cost_pct / stop_dist_pct


# --------------------------------------------------------------------------- #
# Self-tests (pytest-compatible; also run via `python research/v2_signals.py`)
# --------------------------------------------------------------------------- #
def _synthetic(n=600, drift=0.002, vol=0.01, seed=1, freq="4h") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(drift + vol * rng.standard_normal(n)))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + 0.003 * rng.random(n))
    low = np.minimum(open_, close) * (1 - 0.003 * rng.random(n))
    idx = pd.date_range("2024-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1.0}, index=idx)


def test_no_lookahead_truncation_invariance():
    """Signals at row i must be identical whether or not future rows exist."""
    df = _synthetic()
    reg = pd.Series(True, index=df.index)
    full = build_signals(df, reg)
    for i in (250, 300, 411, 599):
        part = build_signals(df.iloc[: i + 1], reg.iloc[: i + 1])
        for col in ("atr", "votes", "target_frac"):
            a, b = full[col].iloc[i], part[col].iloc[i]
            assert (np.isnan(a) and np.isnan(b)) or np.isclose(a, b), f"look-ahead in {col} at {i}"


def test_regime_uses_only_completed_daily_bars():
    idx = pd.date_range("2023-01-02", periods=300, freq="1D", tz="UTC")  # close times
    btc = pd.DataFrame({"close": np.linspace(100, 400, 300)}, index=idx)
    intraday = pd.date_range(idx[250], periods=6, freq="4h", tz="UTC")
    r = regime_on(btc, intraday)
    assert r.all()  # steadily rising, above SMA200, and info is from completed bars only
    # a bar before the first completed daily close has no regime -> False
    before = pd.DatetimeIndex([idx[0] - pd.Timedelta(hours=4)])
    assert not regime_on(btc, before).iloc[0]


def test_direction_sanity_up_vs_down():
    up = _synthetic(drift=0.004, vol=0.004, seed=3)
    dn = _synthetic(drift=-0.004, vol=0.004, seed=3)
    s_up = build_signals(up, pd.Series(True, index=up.index))
    s_dn = build_signals(dn, pd.Series(True, index=dn.index))
    assert s_up["target_frac"].iloc[-1] > 0.5, "bot should be long in a clean uptrend"
    assert s_dn["target_frac"].iloc[-1] == 0.0, "bot must be flat in a clean downtrend"


def test_regime_off_forces_flat():
    up = _synthetic(drift=0.004, vol=0.004, seed=3)
    s = build_signals(up, pd.Series(False, index=up.index))
    assert (s["target_frac"] == 0).all()


def test_forming_candle_is_dropped():
    class FakeEx:
        @staticmethod
        def parse_timeframe(tf): return 4 * 3600
        def milliseconds(self): return 3 * 4 * 3600_000 + 3600_000  # 1h into the 4th bar
        def fetch_ohlcv(self, s, tf, limit=None):
            return [[i * 4 * 3600_000, 1, 2, 0.5, 1.5, 10] for i in range(4)]  # last one is forming
    df = fetch_closed_ohlcv(FakeEx(), "BTC/USDT:USDT", "4h")
    assert len(df) == 3, "forming candle must be excluded"


def test_sizing_and_cost_math():
    p = Params()
    q = position_qty(10_000, 100.0, 2.0, 1.0, Params(max_notional_pct=10.0))
    assert np.isclose(q * p.atr_mult * 2.0, 10_000 * p.risk_pct), "stop loss should equal risk_pct of equity"
    capped = position_qty(10_000, 100.0, 0.01, 1.0, p)
    assert capped * 100.0 <= 10_000 * p.max_notional_pct + 1e-9, "notional cap must bind"
    assert position_qty(10_000, 100.0, 2.0, 0.0, p) == 0.0
    rt = round_trip_cost_pct(0.055, 0.05)
    assert np.isclose(rt, 0.21) and np.isclose(cost_as_fraction_of_risk(rt, 1.0), 0.21)


def test_trailing_stop_never_moves_down():
    df = _synthetic(seed=5)
    atr = _atr_series(df, 14)
    st = trailing_stop(df["close"], atr, 100, 3.0).dropna()
    assert (st.diff().dropna() >= -1e-12).all()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print(f"{len(tests)} tests passed")
