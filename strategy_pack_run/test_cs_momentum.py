"""Look-ahead guards for cs_momentum: signals on row i must not change when future rows are removed."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cs_momentum import CsParams, build_cs_momentum  # noqa: E402
from strategies import daily_regime  # noqa: E402


def _market(n=520, k=8, seed=11):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-01", periods=n, freq="1D", tz="UTC")
    out = {}
    for i in range(k):
        c = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.025, n)))
        o = np.r_[c[0], c[:-1]]
        out[f"S{i}"] = pd.DataFrame(dict(open=o, high=np.maximum(o, c) * 1.005, low=np.minimum(o, c) * 0.995, close=c, volume=1.0), index=idx)
    return out


def test_no_lookahead_truncation():
    mk = _market(); reg = daily_regime(mk["S0"], 50)
    full = build_cs_momentum(mk, reg)
    for cut in (300, 411, 500):
        part = build_cs_momentum({s: df.iloc[:cut] for s, df in mk.items()}, reg.loc[: mk["S0"].index[cut - 1]])
        for s in mk:
            for col in ("enter", "hold", "rank", "atr"):
                assert np.allclose(full[s][col].iloc[:cut].to_numpy(float), part[s][col].to_numpy(float), equal_nan=True), (s, col, cut)


def test_only_top_k_enter_and_only_on_rebalance_bars():
    mk = _market(); reg = daily_regime(mk["S0"], 50); p = CsParams(top_k=2)
    fr = build_cs_momentum(mk, reg, p)
    enters = pd.DataFrame({s: f["enter"] for s, f in fr.items()})
    assert (enters.sum(axis=1) <= p.top_k).all()
    pos = np.arange(len(enters))
    assert not enters.iloc[pos % p.rebalance_every != 0].to_numpy().any()
