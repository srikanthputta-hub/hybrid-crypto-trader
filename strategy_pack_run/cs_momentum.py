"""cs_momentum.py - NEW strategy candidate: long-only CROSS-SECTIONAL momentum rotation, written to engine.py's
generic enter/hold/rank/atr contract (helpers from strategies.py imported, unmodified).

SOURCE: crypto cross-sectional momentum literature found via web search on 2026-10-06 -- e.g. "Cross-sectional Momentum
in Cryptocurrency Markets" (Drogen, Hoffstein, Otte, SSRN 4322637), "Cross-Sectional Momentum in Cryptocurrency: A
Net-of-Costs Replication on Tradable Binance Perpetuals (2020-2026)" (Arefev, SSRN 7404139), and a 2026 comparative study
"Momentum Trading in Cryptocurrencies: A Comparative Study of Time-Series and Cross-Sectional Strategies" (which reports
time-series momentum beating cross-sectional on a risk-adjusted basis). This is this project's OWN long-only,
daily-bar implementation of the general idea -- NOT a reproduction of any paper's system, and none of their reported
returns/Sharpes are trusted or used. The existing trend_ensemble/tsmom are TIME-SERIES (each coin vs its own history);
this ranks coins AGAINST EACH OTHER, a different mechanism.

Rule (all a-priori, one gated run): every `rebalance_every` bars (counted on the shared calendar, no look-ahead), rank
all symbols by trailing `lookback`-bar return. A symbol may ENTER if it is in the top `top_k`, its own return is > 0
(long-only absolute-momentum guard) and the BTC SMA200 regime is ON. A held symbol is released at a rebalance bar once it
drops out of the top `keep_k` (hysteresis to limit churn) or its return turns <= 0. Between rebalances nothing changes.
The engine's 3xATR chandelier remains the loss limit. rank = trailing return (strongest first for scarce capital).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from strategies import _atr, align_regime


@dataclass(frozen=True)
class CsParams:
    lookback: int = 28         # bars (days) of formation return
    rebalance_every: int = 7   # bars between re-rankings
    top_k: int = 4             # enter when ranked in the top k (of ~16)
    keep_k: int = 8            # stay held while ranked in the top keep_k
    atr_len: int = 14


def build_cs_momentum(raw: dict[str, pd.DataFrame], regime: pd.Series, p: CsParams = CsParams()) -> dict[str, pd.DataFrame]:
    idx = pd.DatetimeIndex(sorted(set().union(*[f.index for f in raw.values()])))
    syms = list(raw)
    close = pd.DataFrame({s: raw[s]["close"].reindex(idx) for s in syms})
    ret = close / close.shift(p.lookback) - 1.0
    rk = ret.rank(axis=1, ascending=False, method="first")        # 1 = strongest among symbols with data that bar
    reb = pd.Series((np.arange(len(idx)) % p.rebalance_every) == 0, index=idx)
    reg = align_regime(regime, idx).fillna(0.0) > 0.5

    out = {}
    for s in syms:
        df = raw[s]
        r_s, k_s = ret[s], rk[s]
        enter_sig = reb & (k_s <= p.top_k) & (r_s > 0) & reg
        keep_sig = (k_s <= p.keep_k) & (r_s > 0)
        # hold state is re-evaluated only at rebalance bars, then carried forward until the next one
        hold = keep_sig.where(reb).ffill().fillna(False).astype(bool)
        g = df[["open", "high", "low", "close"]].copy()
        g["atr"] = _atr(df, p.atr_len)
        g["regime"] = reg.reindex(g.index).to_numpy()
        g["enter"] = enter_sig.reindex(g.index).fillna(False).to_numpy()
        g["hold"] = hold.reindex(g.index).fillna(False).to_numpy()
        g["rank"] = r_s.reindex(g.index).fillna(-9.0).to_numpy()
        g["warm"] = (r_s.reindex(g.index).notna() & g["atr"].notna()).to_numpy() & (align_regime(regime, g.index).notna()).to_numpy()
        out[s] = g
    return out
