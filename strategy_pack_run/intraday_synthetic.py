"""intraday_synthetic.py - synthetic intraday universes for the harness tests and for validating
run_intraday_p2.py without touching real evaluation data (docs/intraday_prereg.md §0 rule 3).

Every generator is deterministic in its seed. Bars are built at 15m or 1h resolution with an
open-time index and then passed through intraday_data.frame_from_open_indexed, so they carry the
same `attrs["source"]` tags and close-time convention as the real loaders. Daily bars are always
aggregated from the (synthetic) native 1h with intraday_data.daily_from_1h, never from 15m.

Processes:
  * random_walk: geometric random walk (no edge by construction).
  * regime_switch: the "planted edge" for family 1 -- log returns whose drift switches between
    +mu and -mu in multi-day segments of random length (persistent trends, zero mean), so a
    breakout entered early in an up-segment and exited by a trailing stop earns money while the
    same entries at shifted (random) times do not, and equal-weight buy-and-hold earns ~0.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import intraday_data as idata  # noqa: E402


def ohlcv_from_logpath(open_times: pd.DatetimeIndex, logp: np.ndarray, rng: np.random.Generator,
                       wick: float = 0.002, price0: float = 100.0) -> pd.DataFrame:
    """Bars whose close follows `logp` (log price per bar), open = previous close, wicks random."""
    close = price0 * np.exp(logp)
    open_ = np.r_[close[0], close[:-1]]
    hi = np.maximum(open_, close) * (1 + wick * rng.random(len(close)))
    lo = np.minimum(open_, close) * (1 - wick * rng.random(len(close)))
    vol = 1000.0 * (1 + rng.random(len(close)))
    return pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close, "volume": vol}, index=open_times)


def aggregate_15m_to_1h(df15: pd.DataFrame) -> pd.DataFrame:
    g = df15.resample("1h", label="left", closed="left")
    agg = g.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    cnt = g["close"].count()
    return agg[cnt == 4]


def random_walk_logpath(n: int, rng: np.random.Generator, sigma: float, drift: float = 0.0) -> np.ndarray:
    return np.cumsum(rng.normal(drift, sigma, n))


def regime_switch_logpath(n: int, rng: np.random.Generator, sigma: float, mu: float,
                          seg_lo: int, seg_hi: int) -> np.ndarray:
    drift = np.empty(n)
    i, sign = 0, 1.0 if rng.random() < 0.5 else -1.0
    while i < n:
        L = int(rng.integers(seg_lo, seg_hi))
        drift[i:i + L] = sign * mu
        sign = -sign
        i += L
    return np.cumsum(drift + rng.normal(0, sigma, n))


def make_data(raw_1h: dict[str, pd.DataFrame], tf: str = "1h", raw_15m: dict[str, pd.DataFrame] | None = None,
              end: pd.Timestamp | None = None) -> dict:
    """Assemble a data dict in the same shape as intraday_data.load_universe from open-time
    indexed synthetic frames."""
    native_1h = {c: idata.frame_from_open_indexed(df, "1h", c, end) for c, df in raw_1h.items()}
    if tf == "15m":
        base = {c: idata.frame_from_open_indexed(raw_15m[c], "15m", c, end) for c in raw_15m}
        universe = tuple(sorted(raw_15m))
    else:
        base = {c: native_1h[c] for c in raw_1h}
        universe = tuple(sorted(raw_1h))
    daily = {c: idata.daily_from_1h(native_1h[c]) for c in native_1h}
    return dict(tf=tf, universe=universe, native_1h=native_1h, base=base, daily=daily)


def universe_1h(coins, start: str, n_hours: int, seed: int, kind: str = "random_walk",
                sigma: float = 0.006, mu: float = 0.0, seg_hours: tuple[int, int] = (24 * 4, 24 * 12),
                btc_drift: float = 0.00004, btc_sigma: float = 0.003, gaps: dict | None = None) -> dict:
    """A 1h universe. BTC always gets a gentle up-drift (so the daily SMA(200) regime is ON for
    most of the run) unless btc_drift <= 0; the other coins follow `kind`. `gaps` = {coin: list of
    (start_hour, n_missing)} removes bars (Coinbase-style holes, never filled)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n_hours, freq="1h", tz="UTC")
    raw = {}
    for c in sorted(set(coins) | {"BTC"}):
        if c == "BTC":
            lp = random_walk_logpath(n_hours, rng, btc_sigma, btc_drift)
        elif kind == "random_walk":
            lp = random_walk_logpath(n_hours, rng, sigma)
        elif kind == "regime_switch":
            lp = regime_switch_logpath(n_hours, rng, sigma, mu, *seg_hours)
        else:
            raise ValueError(kind)
        df = ohlcv_from_logpath(idx, lp, rng)
        for g0, gn in (gaps or {}).get(c, []):
            df = df.drop(df.index[g0:g0 + gn])
        raw[c] = df
    return make_data(raw, "1h")


def universe_15m(coins, start: str, n_bars: int, seed: int, sigma: float = 0.003, kind: str = "random_walk",
                 mu: float = 0.0, seg_bars: tuple[int, int] = (96 * 2, 96 * 6), btc_drift: float = 0.00001,
                 btc_sigma: float = 0.0015, mean_revert: float = 0.0) -> dict:
    """A 15m universe (plus its native 1h = complete-hour aggregates). `mean_revert` > 0 adds an
    Ornstein-Uhlenbeck pull toward a slow-moving mean (the family-2 sanity fixture)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n_bars, freq="15min", tz="UTC")
    raw15, raw1h = {}, {}
    for c in sorted(set(coins) | {"BTC"}):
        if c == "BTC":
            lp = random_walk_logpath(n_bars, rng, btc_sigma, btc_drift)
        elif mean_revert > 0:
            x = np.zeros(n_bars)
            eps = rng.normal(0, sigma, n_bars)
            for t in range(1, n_bars):
                x[t] = x[t - 1] * (1 - mean_revert) + eps[t]
            lp = x
        elif kind == "regime_switch":
            lp = regime_switch_logpath(n_bars, rng, sigma, mu, *seg_bars)
        else:
            lp = random_walk_logpath(n_bars, rng, sigma)
        df = ohlcv_from_logpath(idx, lp, rng, wick=0.001)
        raw15[c] = df
        raw1h[c] = aggregate_15m_to_1h(df)
    data = make_data(raw1h, "15m", {c: raw15[c] for c in coins if c in raw15} | {"BTC": raw15["BTC"]})
    data["universe"] = tuple(sorted(set(coins) | {"BTC"}))
    return data
