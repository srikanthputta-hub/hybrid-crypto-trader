"""Look-ahead guard for regime_blend: signals on row i must not change when future rows are removed."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from regime_blend import regime_blend  # noqa: E402
from strategies import daily_regime  # noqa: E402


def _df(n=700, seed=6):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2021-01-01", periods=n, freq="1D", tz="UTC")
    c = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.025, n)))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame(dict(open=o, high=np.maximum(o, c) * 1.006, low=np.minimum(o, c) * 0.994, close=c, volume=1.0), index=idx)


def test_no_lookahead_truncation():
    df = _df(); reg = daily_regime(df, 50)
    full = regime_blend(df, reg)
    for cut in (350, 477, 600):
        part = regime_blend(df.iloc[:cut], reg.loc[: df.index[cut - 1]])
        for col in ("enter", "hold", "rank", "atr"):
            assert np.allclose(full[col].iloc[:cut].to_numpy(float), part[col].to_numpy(float), equal_nan=True), (col, cut)


def test_both_legs_fire():
    df = _df(2500, seed=9); reg = daily_regime(df, 50)
    out = regime_blend(df, reg)
    assert (out["enter"] & (out["leg"] == 1.0)).sum() > 0 and (out["enter"] & (out["leg"] == 2.0)).sum() > 0
