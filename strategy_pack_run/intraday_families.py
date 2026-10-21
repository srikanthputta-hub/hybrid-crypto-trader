"""intraday_families.py - the three pre-registered signal builders (docs/intraday_prereg.md §2-§4,
docs/intraday_harness_changes.md H3).

Pure functions, closed-bar only: every output column at row i uses only rows <= i of the coin's
own decision-bar sequence plus daily inputs whose daily bar CLOSED at or before row i's close
(`intraday_data.align`, forward fill). Lookbacks count the coin's own bars (gaps are not filled).
Indicators come only from src.indicators (sma, atr, adx) and pandas rolling std (ddof=0); no
TA-Lib. `raw_entry` is what the placebo shifts; regime / ADX / eligibility / masks are re-applied
at the landing bar by the runner (prereg §6.3).

Grids (frozen, prereg §2-§4) live here too, as plain tuples; test_intraday_constants.py (AT20)
asserts them against the prereg text.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import intraday_data as idata  # noqa: E402
from src.indicators import atr as _atr  # noqa: E402
from src.indicators import sma as _sma  # noqa: E402

# --------------------------------------------------------------------------- #
# Frozen grids
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SetSpec:
    family: int
    set_no: int           # 1..12
    tf: str               # decision timeframe: "1h", "4h" (F1, F3) / "15m", "1h" (F2)
    params: tuple         # family-specific, see the grids below

    @property
    def set_id(self) -> str:
        return f"F{self.family}-{self.set_no:02d}"

    @property
    def base_tf(self) -> str:
        return "15m" if self.tf == "15m" else "1h"


# Family 1: (TF, N hours, m x daily ATR). N in decision bars = hours / TF hours.
F1_GRID = tuple(
    SetSpec(1, i + 1, tf, (n_hours, m))
    for i, (tf, n_hours, m) in enumerate(
        (tf, n, m) for tf in ("1h", "4h") for n in (72, 240) for m in (1.0, 1.5, 2.0)
    )
)
# Family 2: (TF, n, z_e); time stop = n decision bars; stop = 2.0 x ATR_TF(14); ADX_D < 20.
F2_GRID = tuple(
    SetSpec(2, i + 1, tf, (n, z))
    for i, (tf, n, z) in enumerate((tf, n, z) for tf in ("15m", "1h") for n in (20, 48, 96) for z in (2.0, 2.5))
)
# Family 3: (L 4h bars, K, b); decisions every 4h, base 1h, catastrophe stop 3.0 x ATR_D.
F3_GRID = tuple(
    SetSpec(3, i + 1, "4h", (L, K, b))
    for i, (L, K, b) in enumerate((L, K, b) for L in (18, 42, 84) for K in (3, 5) for b in (0, 2))
)
GRIDS = {1: F1_GRID, 2: F2_GRID, 3: F3_GRID}

F2_STOP_ATR_MULT = 2.0
F2_ADX_MAX = 20.0
F3_STOP_ATR_D_MULT = 3.0


def bars_in(tf: str, hours: int) -> int:
    return int(hours * 60 // idata.TF_MINUTES[tf])


# Grid coordinates used for the R2 neighbour rule (prereg §6.6): each set's index along each axis.
GRID_AXES = {
    1: (("1h", "4h"), (72, 240), (1.0, 1.5, 2.0)),
    2: (("15m", "1h"), (20, 48, 96), (2.0, 2.5)),
    3: ((18, 42, 84), (3, 5), (0, 2)),
}


def grid_coords(spec: SetSpec) -> tuple[int, ...]:
    axes = GRID_AXES[spec.family]
    vals = (spec.tf,) + tuple(spec.params) if spec.family in (1, 2) else tuple(spec.params)
    return tuple(ax.index(v) for ax, v in zip(axes, vals))


def neighbours(spec: SetSpec) -> list[SetSpec]:
    """Sets differing in exactly one parameter by one grid step (TF counts as a parameter)."""
    me = grid_coords(spec)
    out = []
    for other in GRIDS[spec.family]:
        oc = grid_coords(other)
        diffs = [abs(a - b) for a, b in zip(me, oc)]
        if sum(1 for d in diffs if d) == 1 and max(diffs) == 1:
            out.append(other)
    return out


# --------------------------------------------------------------------------- #
# Shared daily alignment
# --------------------------------------------------------------------------- #
def _daily_aligned(daily: pd.DataFrame, index: pd.DatetimeIndex) -> pd.DataFrame:
    di = idata.daily_inputs(daily)
    out = pd.DataFrame(index=index)
    for col in ("atr_d", "adx_d", "atr_pct", "close_d"):
        out[col] = idata.align(di[col], index)
    out["n_daily"] = idata.align(di["n_daily"], index).fillna(0.0)
    return out


def _regime(btc_reg: pd.Series, index: pd.DatetimeIndex) -> np.ndarray:
    return idata.align(btc_reg.astype(float), index).fillna(0.0).to_numpy() > 0.5


# --------------------------------------------------------------------------- #
# Family 1: intraday trend / breakout
# --------------------------------------------------------------------------- #
def family1_signals(df_tf: pd.DataFrame, daily: pd.DataFrame, btc_reg: pd.Series, N: int, m: float | None = None) -> pd.DataFrame:
    """N in decision bars. raw_entry = close_t > max(high_{t-N..t-1}); entry = raw & REG_ON.
    stop0 = close_t - m * ATR_D (latest completed day) when m is given."""
    idx = df_tf.index
    out = pd.DataFrame(index=idx)
    out["close"] = df_tf["close"]
    channel = df_tf["high"].rolling(N).max().shift(1)
    out["channel"] = channel
    out["raw_entry"] = (df_tf["close"] > channel).to_numpy()  # NaN channel compares False
    d = _daily_aligned(daily, idx)
    out["atr_d"] = d["atr_d"]
    out["atr_pct"] = d["atr_pct"]
    out["adx_d"] = d["adx_d"]
    out["n_daily"] = d["n_daily"]
    out["regime_on"] = _regime(btc_reg, idx)
    out["entry"] = out["raw_entry"] & out["regime_on"]
    warm = channel.notna().to_numpy() & idata.wilder_warm(d["n_daily"].to_numpy()) & d["atr_d"].notna().to_numpy()
    out["eligible"] = idata.eligible(d["n_daily"].to_numpy(), warm)
    if m is not None:
        out["stop0"] = df_tf["close"] - m * d["atr_d"]
    return out


# --------------------------------------------------------------------------- #
# Family 2: range mean reversion
# --------------------------------------------------------------------------- #
def family2_signals(df_tf: pd.DataFrame, daily: pd.DataFrame, btc_reg: pd.Series, n: int, z_e: float) -> pd.DataFrame:
    """raw_entry = z_t <= -z_e (no entry if SD == 0); entry = raw & ADX_D < 20 & REG_ON.
    stop = close - 2.0 * ATR_TF(14); target = SMA_n; rr = (target - close) / (close - stop)."""
    idx = df_tf.index
    close = df_tf["close"]
    out = pd.DataFrame(index=idx)
    out["close"] = close
    sma = _sma(close, n)
    sd = close.rolling(n).std(ddof=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (close - sma) / sd.where(sd != 0)
    out["sma"] = sma
    out["sd"] = sd
    out["z"] = z
    out["raw_entry"] = (z <= -z_e).to_numpy()  # NaN (warm-up or SD == 0) compares False
    atr_tf = _atr(df_tf, idata.WILDER_LEN)
    out["atr_tf"] = atr_tf
    out["stop"] = close - F2_STOP_ATR_MULT * atr_tf
    out["target"] = sma
    with np.errstate(divide="ignore", invalid="ignore"):
        out["rr"] = (out["target"] - close) / (close - out["stop"])
    d = _daily_aligned(daily, idx)
    out["atr_d"] = d["atr_d"]
    out["atr_pct"] = d["atr_pct"]
    out["adx_d"] = d["adx_d"]
    out["n_daily"] = d["n_daily"]
    out["regime_on"] = _regime(btc_reg, idx)
    adx_ok = (d["adx_d"] < F2_ADX_MAX).to_numpy() & idata.wilder_warm(d["n_daily"].to_numpy())
    out["adx_ok"] = adx_ok
    out["entry"] = out["raw_entry"] & adx_ok & out["regime_on"]
    n_tf = np.arange(1, len(idx) + 1)
    warm = (
        sma.notna().to_numpy() & sd.notna().to_numpy() & idata.wilder_warm(n_tf)
        & idata.wilder_warm(d["n_daily"].to_numpy()) & d["atr_d"].notna().to_numpy()
    )
    out["eligible"] = idata.eligible(d["n_daily"].to_numpy(), warm)
    return out


# --------------------------------------------------------------------------- #
# Family 3: cross-sectional momentum
# --------------------------------------------------------------------------- #
def family3_state(closes_4h_by_coin: dict[str, pd.Series], daily_by_coin: dict[str, pd.DataFrame],
                  btc_reg: pd.Series, L: int, axis: pd.DatetimeIndex | None = None) -> dict:
    """Per-rebalance state on a global 4h axis: r[c,t] = close_t / close_{t-L} - 1 on the coin's
    OWN 4h sequence (NaN where the coin has no 4h bar at t or is not warm), eligibility, ATR_D,
    atr_pct, regime. The K/b selection depends on holdings and lives in the runner."""
    coins = sorted(closes_4h_by_coin)
    if axis is None:
        axis = pd.DatetimeIndex(sorted(set().union(*[s.index for s in closes_4h_by_coin.values()])))
    r = pd.DataFrame(index=axis, columns=coins, dtype=float)
    elig = pd.DataFrame(False, index=axis, columns=coins)
    atr_d = pd.DataFrame(index=axis, columns=coins, dtype=float)
    atr_pct = pd.DataFrame(index=axis, columns=coins, dtype=float)
    for c in coins:
        s = closes_4h_by_coin[c]
        rc = s / s.shift(L) - 1.0
        d = _daily_aligned(daily_by_coin[c], s.index)
        warm = rc.notna().to_numpy() & idata.wilder_warm(d["n_daily"].to_numpy()) & d["atr_d"].notna().to_numpy()
        el = idata.eligible(d["n_daily"].to_numpy(), warm)
        r[c] = rc.reindex(axis)
        elig[c] = pd.Series(el, index=s.index).reindex(axis).fillna(False).astype(bool)
        atr_d[c] = d["atr_d"].reindex(axis)
        atr_pct[c] = d["atr_pct"].reindex(axis)
    return dict(axis=axis, coins=coins, r=r, eligible=elig, atr_d=atr_d, atr_pct=atr_pct,
                regime_on=_regime(btc_reg, axis))


def family3_ranks(r: np.ndarray, eligible: np.ndarray, coins: list[str]) -> np.ndarray:
    """Rank position (1 = best) of each coin at each rebalance among eligible coins with r > 0,
    ordered by r descending, ties by symbol; 0 = not ranked. r/eligible are (T, S) arrays with
    columns in `coins` order (sorted symbols, so a stable sort on -r keeps ties in symbol order)."""
    T, S = r.shape
    cand = eligible & np.isfinite(r) & (r > 0)
    key = np.where(cand, -r, np.inf)
    order = np.argsort(key, axis=1, kind="stable")
    ranks = np.zeros((T, S), dtype=np.int32)
    pos = np.broadcast_to(np.arange(1, S + 1, dtype=np.int32), (T, S))
    np.put_along_axis(ranks, order, pos, axis=1)
    return np.where(cand, ranks, 0)
