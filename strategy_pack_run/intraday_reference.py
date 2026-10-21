"""intraday_reference.py - an independent, deliberately slow, bar-by-bar reference implementation of
the three pre-registered families, written from docs/intraday_prereg.md §1-§4 alone (playbook
Prompt F; docs/intraday_harness_changes.md AT11 / AT18). Used only by tests.

It shares NO code with intraday_runner / intraday_families / intraday_data: it aggregates its own
daily and 4h bars from the native 1h frames (groupby on the bar-open floor, complete bins only),
computes its own regime / channel / z-score / rank state with plain pandas (Wilder ATR/ADX via
src.indicators, the prereg's named indicator source), and walks every base bar of every coin in
time order with explicit order queues, calling src.risk_engine.RiskEngine directly with
immediate same-bar commitment of each approved quote (cash_available reduced, open_positions_value
increased, exactly like Portfolio.apply_buy would at approval; exits queued on the bar do not
free room).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.indicators import adx as w_adx  # noqa: E402
from src.indicators import atr as w_atr  # noqa: E402
from src.risk_engine import RiskEngine  # noqa: E402

H = pd.Timedelta(hours=1)
Q = pd.Timedelta(minutes=15)
DAY = pd.Timedelta(days=1)
MASKS = [(pd.Timestamp("2023-03-04", tz="UTC"), pd.Timestamp("2023-03-05", tz="UTC")),
         (pd.Timestamp("2024-08-13", tz="UTC"), pd.Timestamp("2024-09-04", tz="UTC")),
         (pd.Timestamp("2025-01-01", tz="UTC"), pd.Timestamp("2025-05-01", tz="UTC"))]


def _agg(frame_close_idx: pd.DataFrame, bar: pd.Timedelta, src: pd.Timedelta) -> pd.DataFrame:
    """Complete bins only, from a close-time-indexed source of bar length `src`."""
    op = frame_close_idx.index - src
    key = op.floor(bar)
    g = frame_close_idx.groupby(key)
    out = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
                volume=("volume", "sum"))
    n = g.size()
    out = out[n == int(bar / src)]
    out.index = out.index + bar
    return out


class Ref:
    def __init__(self, data: dict, coins, start, days, family: int, tf: str, params: tuple, risk_cfg: dict,
                 fee: float, slip: float, touch: bool = False, shift_days: int = 0, log_checks: list | None = None):
        self.coins = sorted(coins)
        self.start = pd.Timestamp(start)
        if self.start.tzinfo is None:
            self.start = self.start.tz_localize("UTC")
        self.end = self.start + days * DAY
        self.family, self.tf, self.params = family, tf, params
        self.fee, self.slip, self.touch = fee, slip, touch
        self.base_dt = Q if tf == "15m" else H
        self.dec_dt = {"15m": Q, "1h": H, "4h": 4 * H}[tf]
        self.risk_cfg = risk_cfg
        self.log = log_checks
        # --- own derived bars ---
        n1h = data["native_1h"]
        self.daily = {c: _agg(n1h[c], DAY, H) for c in set(self.coins) | {"BTC"}}
        btc = self.daily["BTC"]["close"]
        reg = btc > btc.rolling(200).mean()
        self.reg = reg
        self.base = {c: (data["base"][c] if tf == "15m" else n1h[c]) for c in self.coins}
        self.dec = {c: (_agg(n1h[c], 4 * H, H) if tf == "4h" else self.base[c]) for c in self.coins}
        self.dinfo = {}
        for c in self.coins:
            d = self.daily[c]
            self.dinfo[c] = pd.DataFrame({"atr": w_atr(d, 14), "adx": w_adx(d, 14), "close": d["close"],
                                          "n": np.arange(1, len(d) + 1)}, index=d.index)
        # --- per-coin decision-frame features ---
        self.feat = {}
        for c in self.coins:
            df = self.dec[c]
            f = pd.DataFrame(index=df.index)
            f["close"] = df["close"]
            if family == 1:
                N = int(params[0] * H / self.dec_dt)
                f["level"] = [df["high"].iloc[i - N:i].max() if i >= N else np.nan for i in range(len(df))]
                f["raw"] = df["close"] > f["level"]
                f["warm_own"] = ~f["level"].isna()
            elif family == 2:
                n, z_e = params
                f["sma"] = df["close"].rolling(n).mean()
                f["sd"] = df["close"].rolling(n).apply(lambda x: float(np.std(x)), raw=True)
                f["z"] = (df["close"] - f["sma"]) / f["sd"].where(f["sd"] != 0)
                f["raw"] = f["z"] <= -z_e
                f["atr_tf"] = w_atr(df, 14)
                f["warm_own"] = f["sma"].notna() & (np.arange(1, len(df) + 1) >= 114)
            else:
                L = params[0]
                f["r"] = df["close"] / df["close"].shift(L) - 1
                f["raw"] = False
                f["warm_own"] = f["r"].notna()
            self.feat[c] = f
        # decision grid of the window (shift axis)
        self.grid = pd.date_range(self.start, self.end - self.dec_dt, freq=self.dec_dt, tz="UTC")
        self.k = shift_days * int(DAY / self.dec_dt)

    # -------------------------------------------------------------------------------- #
    def daily_at(self, c, t):
        d = self.dinfo[c]
        pos = d.index.searchsorted(t, side="right") - 1
        if pos < 0:
            return None
        return d.iloc[pos]

    def regime_at(self, t):
        pos = self.reg.index.searchsorted(t, side="right") - 1
        return bool(self.reg.iloc[pos]) if pos >= 0 else False

    def eligible(self, c, t):
        di = self.daily_at(c, t)
        if di is None or di["n"] < 114 or di["n"] < 60 or pd.isna(di["atr"]):
            return False
        f = self.feat[c]
        if t not in f.index or not bool(f.loc[t, "warm_own"]):
            return False
        return True

    def mask_ok(self, c, t):
        if self.tf != "15m" or c in ("BTC", "ETH"):
            return True
        n = self.params[0]
        lo, hi = t - (n + 101) * Q, t + (n + 1) * Q
        return not any(lo < e and hi > s for s, e in MASKS)

    def source_time(self, t):
        """Placebo: the decision time whose raw signal lands at t (circular, whole days)."""
        if self.k == 0:
            return t
        g = self.grid.get_loc(t)
        return self.grid[(g - self.k) % len(self.grid)]

    def raw_at(self, c, t):
        ts = self.source_time(t)
        f = self.feat[c]
        return bool(f.loc[ts, "raw"]) if ts in f.index else False

    def r_at(self, c, t):
        ts = self.source_time(t)
        f = self.feat[c]
        return float(f.loc[ts, "r"]) if ts in f.index else float("nan")

    # -------------------------------------------------------------------------------- #
    def run(self):
        rc = self.risk_cfg
        opt = {k: rc[k] for k in ("volatility_scale_floor", "max_new_entries_per_window", "new_entry_window_minutes",
                                  "min_order_quote_usd") if k in rc}
        risk = RiskEngine(rc["starting_cash_usd"], rc["max_position_pct"], rc["max_total_exposure_pct"],
                          rc["max_loss_per_trade_pct"], rc["max_daily_loss_pct"], rc["max_volatility_pct"],
                          rc["min_risk_reward"], rc["cooldown_minutes"], Path("/nonexistent/ref_kill_switch"),
                          self.coins, **opt)
        cash = float(rc["starting_cash_usd"])
        pos = {}        # c -> dict(qty, stop, stop0, target, entry_ref, hc, bars_held, fill_t, ...)
        queue = {}      # c -> ("buy", quote, info) | ("sell", reason)
        trades = []
        last_close = {}
        times = pd.date_range(self.start, self.end, freq=self.base_dt, tz="UTC")
        fam = self.family
        for tau in times:
            open_time = tau - self.base_dt
            # ---- at the open of the bar ending tau ----
            for c in self.coins:
                b = self.base[c]
                if tau not in b.index:
                    continue
                o, hi, lo, cl = (float(b.at[tau, k]) for k in ("open", "high", "low", "close"))
                q = queue.pop(c, None)
                if q is not None and q[0] == "sell":
                    cash += self._close(c, pos.pop(c), o, q[1], open_time, trades)
                elif q is not None and q[0] == "buy":
                    quote, info = q[1], q[2]
                    px = o * (1 + self.slip)
                    notional = quote / (1 + self.fee)
                    cash -= quote
                    info.update(qty=notional / px, entry_raw=o, fill_t=open_time, fee_e=notional * self.fee,
                                hc=-np.inf, bars_held=0, quote=quote)
                    pos[c] = info
                # touch model: intrabar
                if self.touch and c in pos:
                    p = pos[c]
                    st_hit = lo <= p["stop"]
                    tg_hit = (not math.isnan(p["target"])) and hi >= p["target"]
                    if st_hit:
                        reason = "stop_gap" if o <= p["stop"] else ("trail" if p["stop"] > p["stop0"] * (1 + 1e-12) else "stop")
                        cash += self._close(c, pos.pop(c), min(o, p["stop"]), reason, open_time, trades)
                        queue.pop(c, None)
                    elif tg_hit:
                        cash += self._close(c, pos.pop(c), max(o, p["target"]), "target", open_time, trades)
                        queue.pop(c, None)
                last_close[c] = cl
            # ---- at the close tau: exit checks for open positions ----
            is_dec_time = ((tau - self.start) % self.dec_dt) == pd.Timedelta(0) and tau < self.end
            for c in self.coins:
                if c not in pos or tau not in self.base[c].index or c in queue:
                    continue
                p = pos[c]
                cl = float(self.base[c].at[tau, "close"])
                p["bars_held"] += 1
                reason = None
                if not self.touch and cl < p["stop"]:
                    reason = "trail" if p["stop"] > p["stop0"] * (1 + 1e-12) else "stop"
                elif fam == 2 and not self.touch and cl >= p["target"]:
                    reason = "target"
                elif fam == 2 and p["bars_held"] >= self.params[0]:
                    reason = "time"
                dec_bar = is_dec_time and tau in self.dec[c].index
                if reason is None and dec_bar:
                    if fam == 1 and not self.regime_at(tau):
                        reason = "regime"
                    elif fam == 3:
                        rk = self._ranks(tau)
                        K, b = self.params[1], self.params[2]
                        if not self.regime_at(tau):
                            reason = "regime"
                        elif rk.get(c, 0) == 0 or rk[c] > K + b:
                            reason = "rank"
                if fam == 1 and dec_bar:   # trailing update after the checks of this close
                    p["hc"] = max(p["hc"], float(self.dec[c].at[tau, "close"]))
                    di = self.daily_at(c, tau)
                    if di is not None and not pd.isna(di["atr"]):
                        p["stop"] = max(p["stop"], p["hc"] - self.params[1] * float(di["atr"]))
                if reason is not None:
                    queue[c] = ("sell", reason)
            # ---- decisions ----
            if is_dec_time:
                equity = cash + sum(p["qty"] * last_close[c] for c, p in pos.items())
                committed = sum(q[1] for q in queue.values() if q[0] == "buy")
                open_val = sum(p["qty"] * last_close[c] for c, p in pos.items()) + committed
                cash_av = cash - committed
                cands = self._candidates(tau, pos, queue)
                for c, close_t, stop0, target, rr, atr_pct in cands:
                    d = risk.check_new_trade(c, "buy", portfolio_equity=equity, cash_available=cash_av,
                                             entry_price=close_t, stop_loss=stop0, risk_reward=rr,
                                             open_positions_value=open_val, atr_pct=atr_pct,
                                             now=tau.to_pydatetime())
                    if self.log is not None:
                        self.log.append((tau, c, d.allowed, d.max_quote_amount))
                    if not d.allowed:
                        continue
                    risk.record_trade(c, now=tau.to_pydatetime())
                    quote = float(d.max_quote_amount)
                    cash_av -= quote          # immediate commitment (apply_buy-style)
                    open_val += quote
                    queue[c] = ("buy", quote, dict(stop=stop0, stop0=stop0, target=target, entry_ref=close_t,
                                                   signal_t=tau))
            # ---- end of window ----
            if tau == self.end:
                for c in list(pos):
                    if c in self.base and len(self.base[c].loc[:tau]):
                        cash += self._close(c, pos.pop(c), last_close[c], "end", tau, trades, at_close=True)
        self.final_cash = cash
        return trades

    def _ranks(self, t):
        cand = []
        for c in self.coins:
            if t not in self.dec[c].index or not self.eligible(c, t):
                continue
            r = self.r_at(c, t)
            if r == r and r > 0:
                cand.append((-r, c))
        cand.sort()
        return {c: i + 1 for i, (_, c) in enumerate(cand)}

    def _candidates(self, t, pos, queue):
        out = []
        reg = self.regime_at(t)
        if self.family == 3:
            if not reg:
                return out
            rk = self._ranks(t)
            K = self.params[1]
            for c, r in sorted(rk.items(), key=lambda kv: kv[1]):
                if r > K or c in pos or c in queue:
                    continue
                close_t = float(self.dec[c].at[t, "close"])
                di = self.daily_at(c, t)
                out.append((c, close_t, close_t - 3.0 * float(di["atr"]), float("nan"), None,
                            float(di["atr"]) / float(di["close"])))
            return out
        for c in self.coins:
            if c in pos or c in queue or t not in self.dec[c].index:
                continue
            if not (reg and self.raw_at(c, t) and self.eligible(c, t)):
                continue
            close_t = float(self.dec[c].at[t, "close"])
            di = self.daily_at(c, t)
            atr_pct = float(di["atr"]) / float(di["close"])
            if self.family == 1:
                out.append((c, close_t, close_t - self.params[1] * float(di["atr"]), float("nan"), None, atr_pct))
            else:
                if not (float(di["adx"]) < 20 and self.mask_ok(c, t)):
                    continue
                f = self.feat[c].loc[t]
                stop = close_t - 2.0 * float(f["atr_tf"])
                target = float(f["sma"])
                rr = (target - close_t) / (close_t - stop)
                out.append((c, close_t, stop, target, rr, atr_pct))
        return out

    def _close(self, c, p, raw_px, reason, t, trades, at_close=False):
        px = raw_px * (1 - self.slip)
        notional = p["qty"] * px
        fee_x = notional * self.fee
        net = notional - fee_x - p["quote"]
        risk_usd = p["qty"] * (p["entry_ref"] - p["stop0"])
        trades.append(dict(symbol=c, signal_time=p["signal_t"], entry_time=p["fill_t"], exit_time=t,
                           entry_px_raw=p["entry_raw"], exit_px_raw=raw_px, reason=reason, net=net,
                           R=net / risk_usd if risk_usd > 0 else float("nan")))
        return notional - fee_x
