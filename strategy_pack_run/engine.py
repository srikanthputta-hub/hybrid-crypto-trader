"""
engine.py - fee-aware, look-ahead-safe, multi-asset, long-only (spot) backtester.

Execution rules (these are what make results believable):
  * Signals are computed on a CLOSED bar and executed at the NEXT bar's open.
  * Fees and slippage are charged on every fill (entries, signal exits, stop exits).
  * Stops are tested against each bar's open/low. A gap below the stop fills at the open;
    otherwise the stop fills at the stop price (minus slippage). Stop is checked on the entry bar too.
  * The chandelier stop only moves up and is updated at bar close; it applies from the next bar.
  * Position size = equity * risk_pct / (atr_mult * ATR), capped per position and in total,
    and limited by cash. When several entries compete on the same bar, higher `rank` goes first.
  * R-multiple = net P/L (after all costs) / initial risk (qty * entry-to-stop distance).

No shorting, no leverage.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Costs:
    fee: float = 0.0038        # per side, fraction of notional
    slippage: float = 0.0005   # per side, fraction of price
    name: str = "custom"


# Kraken spot fee tiers fetched from kraken.com/features/fee-schedule on 1 Oct 2026
# (Tier 1 0.40/0.80, Tier 2 0.30/0.60, Tier 3 0.22/0.38, Tier 4 0.20/0.35 maker/taker). Verify before relying.
COST_PRESETS = {
    "paper_today_0.10%": Costs(0.0010, 0.0, "paper_today_0.10%"),
    "kraken_T3_maker": Costs(0.0022, 0.0005, "kraken_T3_maker"),
    "kraken_T3_taker": Costs(0.0038, 0.0005, "kraken_T3_taker"),
    "kraken_T1_taker": Costs(0.0080, 0.0005, "kraken_T1_taker"),
}


@dataclass(frozen=True)
class EngineConfig:
    start_equity: float = 10_000.0
    risk_pct: float = 0.0075       # equity risked per position if the initial stop is hit
    max_pos_pct: float = 0.10      # cap on one position's notional
    max_total_pct: float = 0.40    # cap on total invested notional
    atr_mult: float = 3.0          # must match the strategy's chandelier multiple
    min_notional: float = 10.0
    cooldown_bars: int = 0
    periods_per_year: float = 365.0  # 365 for 1d, 365*6 for 4h, 365*24 for 1h
    close_at_end: bool = True


@dataclass
class Result:
    trades: pd.DataFrame
    equity: pd.Series
    exposure: pd.Series
    cfg: EngineConfig
    costs: Costs


def run_backtest(frames: dict[str, pd.DataFrame], costs: Costs, cfg: EngineConfig = EngineConfig()) -> Result:
    syms = list(frames)
    idx = pd.DatetimeIndex(sorted(set().union(*[f.index for f in frames.values()])))
    T, S = len(idx), len(syms)

    def mat(col):
        return np.column_stack([frames[s][col].reindex(idx).to_numpy(float) for s in syms])

    O, H, L, C = mat("open"), mat("high"), mat("low"), mat("close")
    ATR, ENTER, HOLD, RANK = mat("atr"), mat("enter"), mat("hold"), mat("rank")
    fee, slip = costs.fee, costs.slippage

    cash = cfg.start_equity
    qty = np.zeros(S)
    stop = np.full(S, np.nan)
    hh = np.zeros(S)
    entry_px = np.zeros(S)
    entry_fee = np.zeros(S)
    init_risk = np.zeros(S)
    entry_t = np.zeros(S, dtype=int)
    entry_rank = np.zeros(S)
    entry_expo = np.zeros(S)
    pend = np.zeros(S, dtype=int)  # 0 none, 1 enter at next open, -1 exit at next open
    pend_atr = np.zeros(S)
    pend_rank = np.zeros(S)
    cool = np.zeros(S, dtype=int)
    last_close = np.full(S, np.nan)
    trades: list[dict] = []
    equity = np.empty(T)
    expo = np.empty(T)

    def invested() -> float:
        return float(np.nansum(qty * last_close))

    def close_pos(s: int, t: int, raw_px: float, reason: str):
        nonlocal cash
        px = raw_px * (1.0 - slip)
        notional = qty[s] * px
        fee_x = notional * fee
        cash += notional - fee_x
        net = (notional - fee_x) - (qty[s] * entry_px[s] + entry_fee[s])
        trades.append(dict(
            symbol=syms[s], entry_time=idx[entry_t[s]], exit_time=idx[t], entry_px=entry_px[s], exit_px=px,
            qty=qty[s], net_pnl=net, fees=entry_fee[s] + fee_x, R=net / init_risk[s] if init_risk[s] > 0 else np.nan,
            bars_held=t - entry_t[s], reason=reason, entry_rank=entry_rank[s], expo_at_entry=entry_expo[s],
        ))
        qty[s] = 0.0
        stop[s] = np.nan
        cool[s] = cfg.cooldown_bars

    def open_pos(s: int, t: int) -> bool:
        nonlocal cash
        eq = cash + invested()
        price = O[t, s] * (1.0 + slip)
        dist = cfg.atr_mult * pend_atr[s]
        stop_px = max(price - dist, 1e-12)
        dist = price - stop_px
        if dist <= 0 or eq <= 0:
            return False
        q = eq * cfg.risk_pct / dist
        q = min(q, eq * cfg.max_pos_pct / price)
        q = min(q, max(0.0, cfg.max_total_pct * eq - invested()) / price)
        q = min(q, cash / (price * (1.0 + fee)))
        if q * price < cfg.min_notional:
            return False
        entry_expo[s] = (invested() + q * price) / eq  # exposure right after this fill
        cost = q * price
        fee_e = cost * fee
        cash -= cost + fee_e
        qty[s], entry_px[s], stop[s], hh[s] = q, price, stop_px, price
        entry_fee[s], init_risk[s], entry_t[s], entry_rank[s] = fee_e, q * dist, t, pend_rank[s]
        return True

    for t in range(T):
        valid = ~np.isnan(O[t])
        # 1) pending exits fill at this open
        for s in np.where(valid & (pend == -1) & (qty > 0))[0]:
            close_pos(s, t, O[t, s], "signal")
            pend[s] = 0
        # 2) pending entries fill at this open, best rank first
        cands = sorted(np.where(valid & (pend == 1) & (qty == 0))[0], key=lambda s: -pend_rank[s])
        for s in cands:
            open_pos(s, t)
            pend[s] = 0
        # 3) stops (also on the entry bar)
        for s in np.where(valid & (qty > 0) & ~np.isnan(stop))[0]:
            if O[t, s] <= stop[s]:
                close_pos(s, t, O[t, s], "stop_gap")
            elif L[t, s] <= stop[s]:
                close_pos(s, t, stop[s], "stop")
        # 4) bar-close bookkeeping and new decisions (executed at the NEXT open)
        for s in np.where(valid)[0]:
            last_close[s] = C[t, s]
            if qty[s] > 0:
                hh[s] = max(hh[s], C[t, s])
                if not np.isnan(ATR[t, s]):
                    stop[s] = max(stop[s], hh[s] - cfg.atr_mult * ATR[t, s])
                if HOLD[t, s] < 0.5:
                    pend[s] = -1
            else:
                if cool[s] > 0:
                    cool[s] -= 1
                elif ENTER[t, s] > 0.5 and not np.isnan(ATR[t, s]) and ATR[t, s] > 0:
                    pend[s] = 1
                    pend_atr[s] = ATR[t, s]
                    pend_rank[s] = RANK[t, s]
        inv = invested()
        equity[t] = cash + inv
        expo[t] = inv / equity[t] if equity[t] > 0 else 0.0

    if cfg.close_at_end:
        for s in np.where(qty > 0)[0]:
            close_pos(s, T - 1, last_close[s], "end")
        equity[-1] = cash
        expo[-1] = 0.0

    tr = pd.DataFrame(trades)
    return Result(tr, pd.Series(equity, index=idx), pd.Series(expo, index=idx), cfg, costs)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def bootstrap_ci(x: np.ndarray, n: int = 5000, seed: int = 0) -> tuple[float, float]:
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    if len(x) < 5:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    means = rng.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def summarize(res: Result) -> dict:
    tr, eq = res.trades, res.equity
    ppy = res.cfg.periods_per_year
    out: dict = {}
    n = len(tr)
    out["trades"] = n
    if n:
        out["win_rate"] = float((tr["net_pnl"] > 0).mean())
        out["avg_R"] = float(tr["R"].mean())
        lo, hi = bootstrap_ci(tr["R"].to_numpy())
        out["avg_R_ci_low"], out["avg_R_ci_high"] = lo, hi
        wins, losses = tr.loc[tr.net_pnl > 0, "net_pnl"].sum(), -tr.loc[tr.net_pnl <= 0, "net_pnl"].sum()
        out["profit_factor"] = float(wins / losses) if losses > 0 else np.inf
        out["fees_pct_of_start"] = float(tr["fees"].sum() / res.cfg.start_equity)
        out["avg_bars_held"] = float(tr["bars_held"].mean())
    out["total_return"] = float(eq.iloc[-1] / eq.iloc[0] - 1.0) if len(eq) > 1 else 0.0
    years = len(eq) / ppy
    out["cagr"] = float((eq.iloc[-1] / res.cfg.start_equity) ** (1 / years) - 1) if years > 0 and eq.iloc[-1] > 0 else np.nan
    out["max_drawdown"] = float((eq / eq.cummax() - 1.0).min())
    r = eq.pct_change().dropna()
    out["sharpe"] = float(r.mean() / r.std() * np.sqrt(ppy)) if len(r) > 2 and r.std() > 0 else np.nan
    out["avg_exposure"] = float(res.exposure.mean())
    return out


def buy_hold(frames: dict[str, pd.DataFrame], symbols: list[str], start_equity: float, ppy: float) -> dict:
    """Equal-weight buy-and-hold (no rebalancing) over the engine's window."""
    idx = pd.DatetimeIndex(sorted(set().union(*[f.index for f in frames.values()])))
    curves = []
    for s in symbols:
        c = frames[s]["close"].reindex(idx).ffill().bfill()
        curves.append(c / c.iloc[0])
    eq = start_equity * pd.concat(curves, axis=1).mean(axis=1)
    years = len(eq) / ppy
    r = eq.pct_change().dropna()
    return dict(
        total_return=float(eq.iloc[-1] / eq.iloc[0] - 1),
        cagr=float((eq.iloc[-1] / start_equity) ** (1 / years) - 1) if years > 0 else np.nan,
        max_drawdown=float((eq / eq.cummax() - 1).min()),
        sharpe=float(r.mean() / r.std() * np.sqrt(ppy)) if r.std() > 0 else np.nan,
    )


def placebo_baseline(frames, costs: Costs, cfg: EngineConfig, n_sims: int = 100, seed: int = 0) -> pd.DataFrame:
    """Time-shift placebo: re-run the SAME signals (enter/hold/rank) circularly shifted by a random offset
    (one offset shared by all symbols, so clustering, frequency and cross-coin structure are preserved) against the
    unchanged prices. If the real signals carry information, they should beat most shifted copies."""
    rng = np.random.default_rng(seed)
    T = len(pd.DatetimeIndex(sorted(set().union(*[f.index for f in frames.values()]))))
    rows = []
    for _ in range(n_sims):
        k = int(rng.integers(int(T * 0.10), int(T * 0.90)))
        sim = {}
        for s, f in frames.items():
            g = f.copy()
            for col in ("enter", "hold", "rank"):
                g[col] = np.roll(f[col].to_numpy(), k)
            sim[s] = g
        m = summarize(run_backtest(sim, costs, cfg))
        rows.append(dict(total_return=m["total_return"], avg_R=m.get("avg_R", np.nan), trades=m["trades"]))
    return pd.DataFrame(rows)
