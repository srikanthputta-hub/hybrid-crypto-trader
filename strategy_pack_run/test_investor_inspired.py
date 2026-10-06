"""Look-ahead guards for investor_inspired: signals on row i must not change when future rows are removed."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from investor_inspired import concentrate_top_n, conviction_rank, dalio_smooth_trend  # noqa: E402
from strategies import daily_regime  # noqa: E402


def _market(n=600, seed=3, k=4):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-01", periods=n, freq="1D", tz="UTC")
    out = {}
    for i in range(k):
        c = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.02, n)))
        o = np.r_[c[0], c[:-1]]
        out[f"S{i}"] = pd.DataFrame(dict(open=o, high=np.maximum(o, c) * 1.005, low=np.minimum(o, c) * 0.995, close=c, volume=1.0), index=idx)
    return out


def test_dalio_no_lookahead():
    mk = _market(); df = mk["S0"]; reg = daily_regime(df, 50)
    full = dalio_smooth_trend(df, reg)
    cut = 420
    part = dalio_smooth_trend(df.iloc[:cut], reg.loc[: df.index[cut - 1]])
    for col in ("enter", "hold", "rank", "atr"):
        assert np.allclose(full[col].iloc[:cut].to_numpy(float), part[col].to_numpy(float), equal_nan=True), col


def test_concentration_no_lookahead_and_top1():
    mk = _market(); reg = daily_regime(mk["S0"], 50)
    fr = {s: conviction_rank(df, reg) for s, df in mk.items()}
    top = concentrate_top_n(fr, 1)
    cut = 420
    fr_p = {s: conviction_rank(df.iloc[:cut], reg.loc[: df.index[cut - 1]]) for s, df in mk.items()}
    top_p = concentrate_top_n(fr_p, 1)
    for s in mk:
        assert np.array_equal(top[s]["enter"].iloc[:cut].to_numpy(bool), top_p[s]["enter"].to_numpy(bool)), s
    stacked = pd.DataFrame({s: top[s]["enter"] for s in mk})
    assert (stacked.sum(axis=1) <= 1).all()
