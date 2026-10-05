"""Look-ahead guard for mtf_confirm: signals on row i must not change when future rows are removed."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mtf_confirm import mtf_trend, weekly_uptrend  # noqa: E402
from strategies import daily_regime  # noqa: E402


def _df(n=700, seed=4):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2021-01-02", periods=n, freq="1D", tz="UTC")
    c = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.02, n)))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame(dict(open=o, high=np.maximum(o, c) * 1.005, low=np.minimum(o, c) * 0.995, close=c, volume=1.0), index=idx)


def test_mtf_no_lookahead_truncation():
    df = _df(); reg = daily_regime(df, 50)
    full = mtf_trend(df, reg)
    for cut in (400, 419, 523):   # cuts landing on different weekdays
        part = mtf_trend(df.iloc[:cut], reg.loc[: df.index[cut - 1]])
        for col in ("enter", "hold", "rank", "atr"):
            assert np.allclose(full[col].iloc[:cut].to_numpy(float), part[col].to_numpy(float), equal_nan=True), (col, cut)


def test_weekly_value_constant_within_week():
    df = _df()
    wk = weekly_uptrend(df, 10).dropna()
    # forward-filled weekly flag can change at most once per 7 daily bars
    assert (wk.astype(int).diff().abs().rolling(7).sum().dropna() <= 1).all()
