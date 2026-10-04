"""Look-ahead guard for overreaction_fade: signals on row i must not change when future rows are removed."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from overreaction_fade import FadeParams, overreaction_fade  # noqa: E402
from strategies import daily_regime  # noqa: E402


def _market(n=500, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-01", periods=n, freq="1D", tz="UTC")
    c = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.03, n)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + rng.uniform(0, 0.01, n))
    l = np.minimum(o, c) * (1 - rng.uniform(0, 0.01, n))
    return pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=1.0), index=idx)


def test_no_lookahead_truncation():
    df = _market()
    reg = daily_regime(df, 50)
    full = overreaction_fade(df, reg)
    cut = 350
    part = overreaction_fade(df.iloc[:cut], reg.loc[: df.index[cut - 1]])
    for col in ("enter", "hold", "rank", "atr"):
        a = full[col].iloc[:cut].to_numpy(float)
        b = part[col].to_numpy(float)
        assert np.allclose(a, b, equal_nan=True), col


def test_produces_some_signals():
    df = _market(900, seed=5)
    out = overreaction_fade(df, daily_regime(df, 50), FadeParams(z_entry=1.5))
    assert out["enter"].sum() > 0
