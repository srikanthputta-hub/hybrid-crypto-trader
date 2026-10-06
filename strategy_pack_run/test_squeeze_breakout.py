"""Look-ahead guard for squeeze_breakout: signals on row i must not change when future rows are removed."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from squeeze_breakout import squeeze_breakout  # noqa: E402
from strategies import daily_regime  # noqa: E402


def _df(n=800, seed=8):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2021-01-01", periods=n, freq="1D", tz="UTC")
    vol = np.where((np.arange(n) // 60) % 3 == 0, 0.004, 0.03)       # alternate calm / wild regimes -> squeezes then breakouts
    c = 100 * np.exp(np.cumsum(rng.normal(0.001, vol)))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame(dict(open=o, high=np.maximum(o, c) * 1.004, low=np.minimum(o, c) * 0.996, close=c, volume=1.0), index=idx)


def test_no_lookahead_truncation():
    df = _df(); reg = daily_regime(df, 50)
    full = squeeze_breakout(df, reg)
    for cut in (400, 513, 700):
        part = squeeze_breakout(df.iloc[:cut], reg.loc[: df.index[cut - 1]])
        for col in ("enter", "hold", "rank", "atr"):
            assert np.allclose(full[col].iloc[:cut].to_numpy(float), part[col].to_numpy(float), equal_nan=True), (col, cut)


def test_produces_entries():
    df = _df(1500, seed=2); reg = daily_regime(df, 50)
    assert squeeze_breakout(df, reg)["enter"].sum() > 0
