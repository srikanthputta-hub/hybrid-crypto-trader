"""intraday_stats.py - splits, champion rule, block bootstrap, folds, Sharpe, benchmark, placebo
shifts, Deflated Sharpe and the gate / verdict logic of docs/intraday_prereg.md §6
(docs/intraday_harness_changes.md H6). Every constant is frozen here and asserted against the
prereg text by test_intraday_constants.py (AT20). No CLI can change any of them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

NS_DAY = 86_400 * 10**9

# --------------------------------------------------------------------------- #
# Frozen constants (prereg §6)
# --------------------------------------------------------------------------- #
B_START = pd.Timestamp("2021-10-01 00:00", tz="UTC")
B_END = pd.Timestamp("2026-10-20 00:00", tz="UTC")
B_DAYS = 1845
IS_FRACTION = 0.7
CUT_B = pd.Timestamp("2025-04-14 00:00", tz="UTC")   # = B_START + floor(0.7 * 1845) days
FOLDS = (
    ("F1", pd.Timestamp("2021-10-01", tz="UTC"), pd.Timestamp("2023-01-01", tz="UTC")),
    ("F2", pd.Timestamp("2023-01-01", tz="UTC"), pd.Timestamp("2024-01-01", tz="UTC")),
    ("F3", pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2025-01-01", tz="UTC")),
    ("F4", pd.Timestamp("2025-01-01", tz="UTC"), pd.Timestamp("2026-01-01", tz="UTC")),
    ("F5", pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-10-20", tz="UTC")),
)
FOLD_MIN_TRADES = 30
FOLDS_MIN_SCORED = 3
FOLDS_MAX_LOSING = 1
MIN_IS_TRADES = 150
TIE_EPS = 1e-9
G1_MIN_TRADES = 300
G1_MIN_BLOCKS = 100
G3_MIN_OOS = 60
N_BOOT = 50_000
BOOT_SEED = 1
N_TRIALS_G2 = 36
G2_ALPHA = 0.025 / N_TRIALS_G2
G2_PERCENTILE = 0.0694          # the prereg's printed value (100 * 0.025 / 36 = 0.06944..., rounded down)
INFO_PERCENTILES = (0.139, 2.5, 97.5)
INFO_BLOCK_DAYS = 7
N_PLACEBO = 200
PLACEBO_SEED = 0
PLACEBO_LO, PLACEBO_HI = 184, 1660
G4_MAX_DRAWS_GE = 2             # strictly beat >= 198 of 200
G4_FAMILIES = 3
SHARPE_DAYS = 365.0
TRADES_PCTS = (G2_PERCENTILE,) + INFO_PERCENTILES


def oos_cut(start: pd.Timestamp = B_START, days: int = B_DAYS) -> pd.Timestamp:
    return start + pd.Timedelta(days=math.floor(IS_FRACTION * days))


# --------------------------------------------------------------------------- #
# Splits, champion, folds
# --------------------------------------------------------------------------- #
def purged_split(entry_ns: np.ndarray, exit_ns: np.ndarray, cut: pd.Timestamp = CUT_B):
    """(is_mask, oos_mask, straddle_mask): IS score = entry AND exit before the cut; OOS = entry at
    or after the cut; straddlers (entry before, exit at/after) count in neither."""
    c = cut.value
    e, x = np.asarray(entry_ns, dtype=np.int64), np.asarray(exit_ns, dtype=np.int64)
    is_m = (e < c) & (x < c)
    oos = e >= c
    return is_m, oos, ~is_m & ~oos


def champion(rows: list[dict]) -> dict | None:
    """rows: {set_no, is_n, is_avgR}. Highest purged IS avg R among sets with >= 150 purged IS
    trades; ties within 1e-9: more IS trades, then lower set number. None if no set qualifies."""
    ok = [r for r in rows if r["is_n"] >= MIN_IS_TRADES and np.isfinite(r["is_avgR"])]
    if not ok:
        return None
    best = max(r["is_avgR"] for r in ok)
    tied = [r for r in ok if r["is_avgR"] >= best - TIE_EPS]
    tied.sort(key=lambda r: (-r["is_n"], r["set_no"]))
    return tied[0]


def folds(entry_ns: np.ndarray, R: np.ndarray) -> list[dict]:
    out = []
    e = np.asarray(entry_ns, dtype=np.int64)
    for name, a, b in FOLDS:
        m = (e >= a.value) & (e < b.value)
        n = int(m.sum())
        avg = float(np.mean(R[m])) if n else float("nan")
        scored = n >= FOLD_MIN_TRADES
        out.append(dict(fold=name, n=n, avgR=avg, scored=scored, losing=bool(scored and avg < 0)))
    return out


def fold_of(entry_ns: np.ndarray) -> np.ndarray:
    e = np.asarray(entry_ns, dtype=np.int64)
    out = np.full(len(e), "", dtype=object)
    for name, a, b in FOLDS:
        out[(e >= a.value) & (e < b.value)] = name
    return out


# --------------------------------------------------------------------------- #
# Block bootstrap
# --------------------------------------------------------------------------- #
def block_days_for(entry_ns: np.ndarray, exit_ns: np.ndarray) -> int:
    """L_b = max(1, ceil(median holding period in days))."""
    if len(entry_ns) == 0:
        return 1
    hold = (np.asarray(exit_ns, dtype=np.int64) - np.asarray(entry_ns, dtype=np.int64)) / NS_DAY
    return max(1, int(math.ceil(float(np.median(hold)))))


def block_ids(entry_ns: np.ndarray, start: pd.Timestamp, block_days: int) -> np.ndarray:
    return (np.asarray(entry_ns, dtype=np.int64) - start.value) // (block_days * NS_DAY)


def _resample_means(sums: np.ndarray, counts: np.ndarray, n: int, seed: int, chunk: int = 2000) -> np.ndarray:
    rng = np.random.default_rng(seed)
    nb = len(sums)
    out = np.empty(n)
    done = 0
    while done < n:
        m = min(chunk, n - done)
        idx = rng.integers(0, nb, size=(m, nb))
        out[done:done + m] = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
        done += m
    return out


def block_bootstrap(R: np.ndarray, entry_ns: np.ndarray, start: pd.Timestamp, block_days: int,
                    n: int = N_BOOT, seed: int = BOOT_SEED, percentiles=TRADES_PCTS) -> dict:
    """Non-overlapping calendar blocks of `block_days` UTC days from `start`; each resample draws,
    with replacement, as many blocks as there are blocks containing >= 1 entry, from those blocks;
    statistic = total R / total trades of the drawn blocks. The iid comparison (design effect)
    resamples trades with replacement, same n and seed scheme (its own default_rng(seed))."""
    R = np.asarray(R, dtype=float)
    if len(R) == 0:
        return dict(n=0, n_blocks=0, block_days=block_days, pct={p: float("nan") for p in percentiles},
                    var_block=float("nan"), var_iid=float("nan"), DE=float("nan"), n_eff=float("nan"))
    ids = block_ids(entry_ns, start, block_days)
    uniq, inv = np.unique(ids, return_inverse=True)
    sums = np.bincount(inv, weights=R).astype(float)
    counts = np.bincount(inv).astype(float)
    means = _resample_means(sums, counts, n, seed)
    iid = _resample_means(R.copy(), np.ones(len(R)), n, seed)
    var_b, var_i = float(np.var(means, ddof=1)), float(np.var(iid, ddof=1))
    de = var_b / var_i if var_i > 0 else float("nan")
    return dict(n=len(R), n_blocks=len(uniq), block_days=block_days,
                pct={p: float(np.percentile(means, p, method="linear")) for p in percentiles},
                var_block=var_b, var_iid=var_i, DE=de, n_eff=(len(R) / de) if de and de == de else float("nan"),
                per_block_max=int(counts.max()), per_block_median=float(np.median(counts)))


# --------------------------------------------------------------------------- #
# Sharpe, drawdown, benchmark
# --------------------------------------------------------------------------- #
def daily_sharpe(equity_daily: np.ndarray) -> float:
    eq = np.asarray(equity_daily, dtype=float)
    r = eq[1:] / eq[:-1] - 1.0
    if len(r) < 3:
        return float("nan")
    sd = float(np.std(r, ddof=1))
    return float(np.mean(r) / sd * math.sqrt(SHARPE_DAYS)) if sd > 0 else float("nan")


def max_drawdown(equity: np.ndarray) -> tuple[float, int, int]:
    eq = np.asarray(equity, dtype=float)
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1.0
    trough = int(np.argmin(dd))
    peak_i = int(np.argmax(eq[: trough + 1])) if trough > 0 else 0
    return float(dd[trough]), peak_i, trough


def buy_hold_daily(daily_close: np.ndarray, first_ok: np.ndarray, masked_day: np.ndarray | None = None,
                   start_equity: float = 10_000.0) -> np.ndarray:
    """Equal-weight buy-and-hold over daily closes (S, D+1): one sleeve of start_equity/S per coin,
    held in cash until the coin's first eligible daily close `first_ok[s]` (index), then invested,
    no rebalancing, no costs. `masked_day[s, i]` (15m runs) zeroes the sleeve's return over day i
    (from close i to close i+1). Closes are forward filled, so a missing day returns 0."""
    S, D1 = daily_close.shape
    sleeves = np.full((S, D1), start_equity / S)
    for s in range(S):
        f = int(first_ok[s])
        if f < 0 or f >= D1:
            continue
        px = pd.Series(daily_close[s]).ffill().to_numpy()
        ret = np.zeros(D1)
        with np.errstate(divide="ignore", invalid="ignore"):
            rr = px[1:] / px[:-1] - 1.0
        rr = np.where(np.isfinite(rr), rr, 0.0)
        ret[1:] = rr
        ret[: f + 1] = 0.0
        if masked_day is not None:
            ret[1:] = np.where(masked_day[s, :-1], 0.0, ret[1:])
        sleeves[s] = start_equity / S * np.cumprod(1.0 + ret)
    return sleeves.sum(axis=0)


# --------------------------------------------------------------------------- #
# Placebo shifts
# --------------------------------------------------------------------------- #
def placebo_days(seed: int = PLACEBO_SEED, n: int = N_PLACEBO, lo: int = PLACEBO_LO, hi: int = PLACEBO_HI) -> list[int]:
    """The 200 whole-day shifts, d = rng.integers(184, 1660) per draw from default_rng(0); generated
    once in the main process, shared by every set and family."""
    rng = np.random.default_rng(seed)
    return [int(rng.integers(lo, hi)) for _ in range(n)]


def shift_raw(raw: np.ndarray, d: int, bars_per_day: int) -> np.ndarray:
    """Circular shift by d whole days along the decision axis (axis 1 of an (S, Td) array), the
    SAME shift for every coin (row)."""
    return np.roll(raw, d * bars_per_day, axis=1)


def g4_counts(real_avgR: float, real_ret: float, draws: list[dict | None]) -> dict:
    """draws[i] = {avgR, total_return} of draw i's champion, or None when the draw had no set with
    >= 150 purged IS trades. CONSERVATIVE reading (the prereg does not say): a draw without a
    champion counts as a draw the real champion does NOT strictly beat."""
    ge_R = sum(1 for x in draws if x is None or not (real_avgR > x["avgR"]))
    ge_ret = sum(1 for x in draws if x is None or not (real_ret > x["total_return"]))
    none = sum(1 for x in draws if x is None)
    return dict(ge_avgR=ge_R, ge_return=ge_ret, no_champion=none,
                p_avgR=(1 + ge_R) / (len(draws) + 1), p_return=(1 + ge_ret) / (len(draws) + 1))


def deflated_sharpe(sr: float, n_trials: int, T: int, skew: float, kurt: float, var_sr: float) -> float:
    """Bailey & Lopez de Prado DSR (information only). sr, var_sr per-period (non-annualised)."""
    from scipy.stats import norm

    if not (np.isfinite(sr) and np.isfinite(var_sr) and var_sr > 0 and T > 1):
        return float("nan")
    g = 0.5772156649
    sr0 = math.sqrt(var_sr) * ((1 - g) * norm.ppf(1 - 1 / n_trials) + g * norm.ppf(1 - 1 / (n_trials * math.e)))
    den = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if den <= 0:
        return float("nan")
    return float(norm.cdf((sr - sr0) * math.sqrt(T - 1) / math.sqrt(den)))


# --------------------------------------------------------------------------- #
# Gates and verdict
# --------------------------------------------------------------------------- #
@dataclass
class GateInputs:
    n_trades: int
    n_blocks: int
    g2_lower: float
    oos_n: int
    oos_avgR: float
    folds: list
    g4: dict
    ret_taker: float
    sharpe: float
    bench_sharpe: float
    ret_stress: float
    a_avgR: float
    a_ret: float
    neighbour_avgR: list


def evaluate_gates(g: GateInputs) -> dict:
    scored = [f for f in g.folds if f["scored"]]
    losing = [f for f in scored if f["losing"]]
    g3_count_fail = g.oos_n < G3_MIN_OOS or len(scored) < FOLDS_MIN_SCORED
    g3_subst_fail = len(losing) > FOLDS_MAX_LOSING or (g.oos_n > 0 and not (g.oos_avgR > 0))
    nb = [x for x in g.neighbour_avgR]
    need = math.ceil(len(nb) / 2)
    res = {
        "G1": g.n_trades >= G1_MIN_TRADES and g.n_blocks >= G1_MIN_BLOCKS,
        "G2": bool(g.g2_lower > 0),
        "G3": (not g3_count_fail) and (not g3_subst_fail),
        "G4": g.g4["ge_avgR"] <= G4_MAX_DRAWS_GE and g.g4["ge_return"] <= G4_MAX_DRAWS_GE,
        "G5": bool(g.ret_taker > 0 and g.sharpe > g.bench_sharpe and g.ret_stress > 0),
        "R1": bool(g.a_avgR > 0 and g.a_ret > 0),
        "R2": sum(1 for x in nb if x > 0) >= need,
    }
    res["G3_count_only"] = bool(g3_count_fail and not g3_subst_fail)
    res["R2_detail"] = dict(neighbours=len(nb), positive=sum(1 for x in nb if x > 0), needed=need)
    res["G3_detail"] = dict(scored=len(scored), losing=len(losing), oos_n=g.oos_n, oos_avgR=g.oos_avgR)
    return res


def verdict(gates: dict | None, integrity_ok: bool = True) -> str:
    """VOID > PASS > INCONCLUSIVE > FAIL (prereg §6.7). gates None = no champion (FAIL)."""
    if not integrity_ok:
        return "VOID"
    if gates is None:
        return "FAIL"
    keys = ("G1", "G2", "G3", "G4", "G5", "R1", "R2")
    failed = [k for k in keys if not gates[k]]
    if not failed:
        return "PASS"
    allowed = {"G1"} | ({"G3"} if gates.get("G3_count_only") else set())
    if set(failed) <= allowed and ("G1" in failed or "G3" in failed):
        return "INCONCLUSIVE"
    return "FAIL"
