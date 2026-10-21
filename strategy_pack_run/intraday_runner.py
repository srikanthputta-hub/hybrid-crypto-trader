"""intraday_runner.py - the intraday P2 simulator (docs/intraday_harness_changes.md H4/H5;
docs/intraday_prereg.md §1, §5).

Execution model (prereg §1.1, §1.4), identical for every family:
  * Decisions on CLOSED decision bars (15m / 1h / 4h); entries execute at the coin's NEXT
    available base-bar open (1h for 1h/4h sets, 15m for 15m sets). Never at the signal bar.
  * Gating exit model "close": every exit condition is evaluated on a base close and executes at
    the coin's next available base open (a gap through the stop fills at that open).
  * Sensitivity exit model "touch": stop fills on touch at min(open, stop), fixed target at
    max(open, target), stop first when both touch in one base bar; 4h positions walk 1h bars.
  * Sizing: the real src.risk_engine.RiskEngine built from config/config.yaml's risk block,
    check_new_trade at the decision close with P6 same-bar accounting (gross quote committed by
    fills queued earlier, on this bar or still unexecuted, is added to open_positions_value and
    subtracted from cash_available; exits queued on this bar do not free room), then
    record_trade(symbol, now). Spend = max_quote_amount, never resized.
  * Costs on every fill: buy at open*(1+slip), sell at px*(1-slip); fee = fee rate x FILLED
    notional; the cash spent on an entry is exactly the approved quote (notional = quote/(1+fee)).

Implementation (performance, not semantics): the exit of a position depends only on the coin's
own prices, the daily regime and (family 3) the cross-sectional rank state -- never on other
positions -- so it is computed once, at approval, by a vectorised forward scan of that coin's
arrays (`_exit_*`). The portfolio loop then only visits decision bars that carry a candidate and
replays fills/exits as timed cash events. test_intraday_reference.py checks this against an
independent bar-by-bar loop written from the prereg text (AT11/AT18), trade for trade.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import intraday_data as idata  # noqa: E402
import intraday_families as fam  # noqa: E402
from engine import COST_PRESETS, Costs  # noqa: E402
from src.risk_engine import RiskEngine  # noqa: E402
from tiered_slippage import tiered_costs_series_by_symbol  # noqa: E402

HARNESS_VERSION = "intraday-prereg-v1 + p6-same-bar-exposure-v1"
GATING_COST = "kraken_T3_taker"
STRESS_COSTS = Costs(0.00475, 0.000625, "kraken_T3_taker_+25%")
MAKER_FEE = 0.0022
MAKER_TRADE_THROUGH = 0.0005
TIER_VOLUME_DIVISOR = 3.0
TIER_MEDIAN_DAYS = 30
NS_DAY = 86_400 * 10**9

# A path that is never created: the kill switch is an operational control of the live bot, not a
# strategy rule, so a KILL_SWITCH file left in the repo root must not silently empty a backtest.
_NO_KILL_SWITCH = _THIS_DIR / ".intraday_harness_kill_switch_never_created"

RISK_KEYS_REQUIRED = (
    "starting_cash_usd", "max_position_pct", "max_total_exposure_pct", "max_loss_per_trade_pct",
    "max_daily_loss_pct", "max_volatility_pct", "min_risk_reward", "cooldown_minutes",
)
RISK_KEYS_OPTIONAL = ("volatility_scale_floor", "max_new_entries_per_window", "new_entry_window_minutes",
                      "min_order_quote_usd")


def load_risk_cfg() -> dict:
    """The unchanged `risk:` block of config/config.yaml (prereg §1.4)."""
    from src.config import load_config

    return dict(load_config().risk)


def make_risk_engine(risk_cfg: dict, symbols) -> RiskEngine:
    if _NO_KILL_SWITCH.exists():
        raise RuntimeError(f"{_NO_KILL_SWITCH} must never exist")
    kw = {k: risk_cfg[k] for k in RISK_KEYS_REQUIRED}
    kw.update({k: risk_cfg[k] for k in RISK_KEYS_OPTIONAL if k in risk_cfg})
    return RiskEngine(kill_switch_path=_NO_KILL_SWITCH, symbol_whitelist=list(symbols), **kw)


# --------------------------------------------------------------------------- #
# Market: the universe on fixed calendar grids
# --------------------------------------------------------------------------- #
class Market:
    """Base-bar arrays on the calendar grid start, start+Δb, ..., start+days (Tb = days*bpd+1;
    index 0 is the bar closing at the window start, used only as a decision anchor). Arrays are
    (S, Tb), one contiguous row per coin, NaN where Coinbase emitted no bar."""

    def __init__(self, data: dict, start: pd.Timestamp, days: int, coins=None):
        self.data = data
        self.base_tf = data["tf"]
        self.coins = tuple(sorted(coins if coins is not None else data["universe"]))
        self.S = len(self.coins)
        self.start = pd.Timestamp(start)
        self.days = int(days)
        self.end = self.start + pd.Timedelta(days=self.days)
        self.step = idata._tf_delta(self.base_tf).value
        self.bpd = NS_DAY // self.step
        self.Tb = self.days * self.bpd + 1
        self.base_ns = self.start.value + np.arange(self.Tb, dtype=np.int64) * self.step
        idx = pd.DatetimeIndex(self.base_ns, tz="UTC")
        base_frames = {c: data["base"][c] for c in self.coins}
        idata.assert_sources(self.base_tf, base_frames, [], [data["daily"][c] for c in self.coins])
        shape = (self.S, self.Tb)
        self.O, self.H, self.L, self.C = (np.full(shape, np.nan) for _ in range(4))
        for i, c in enumerate(self.coins):
            f = base_frames[c].reindex(idx)
            self.O[i], self.H[i], self.L[i], self.C[i] = (f[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        self.has = ~np.isnan(self.C)
        self.Cff = pd.DataFrame(self.C.T).ffill().to_numpy().T.copy()
        self.bar_idx = [np.flatnonzero(self.has[i]) for i in range(self.S)]
        self.next_bar = np.empty(shape, dtype=np.int64)
        ar = np.arange(self.Tb)
        for i in range(self.S):
            b = self.bar_idx[i]
            pos = np.searchsorted(b, ar, side="right")
            nb = np.full(self.Tb, self.Tb, dtype=np.int64)
            ok = pos < len(b)
            nb[ok] = b[pos[ok]]
            self.next_bar[i] = nb
        self.last_bar = np.array([b[-1] if len(b) else -1 for b in self.bar_idx], dtype=np.int64)
        self.btc_reg = idata.btc_regime(data["daily"]["BTC"])
        self._daily_ns = self.base_ns[:: self.bpd]  # UTC daily closes start..end

    def decision_axis(self, dec_tf: str) -> tuple[int, np.ndarray]:
        step = idata._tf_delta(dec_tf).value
        r = step // self.step
        Td = self.days * (NS_DAY // step)
        return r, self.start.value + np.arange(Td, dtype=np.int64) * step

    def decision_frames(self, dec_tf: str) -> dict:
        if dec_tf == self.base_tf:
            return {c: self.data["base"][c] for c in self.coins}
        if dec_tf == "4h" and self.base_tf == "1h":
            cache = self.data.setdefault("_4h", {})
            for c in self.coins:
                if c not in cache:
                    cache[c] = idata.four_hour_from_1h(self.data["native_1h"][c])
            return {c: cache[c] for c in self.coins}
        raise idata.MixedSourceError(f"no decision frames for dec_tf={dec_tf} on a {self.base_tf} base")


# --------------------------------------------------------------------------- #
# Per-set decision-axis arrays
# --------------------------------------------------------------------------- #
class SetData:
    """All decision-axis inputs of one parameter set, (S, Td) per field, computed once and shared
    by the real run, every cost/exit/fill view and every placebo draw."""

    def __init__(self, market: Market, spec: fam.SetSpec):
        self.spec = spec
        self.family = spec.family
        if spec.base_tf != market.base_tf:
            raise idata.MixedSourceError(f"{spec.set_id} needs a {spec.base_tf} base, market is {market.base_tf}")
        self.r, self.dec_ns = market.decision_axis(spec.tf)
        self.Td = len(self.dec_ns)
        self.bpd_dec = NS_DAY // (self.r * market.step)
        dec_index = pd.DatetimeIndex(self.dec_ns, tz="UTC")
        frames = market.decision_frames(spec.tf)
        idata.assert_sources(market.base_tf, [market.data["base"][c] for c in market.coins], frames,
                             [market.data["daily"][c] for c in market.coins])
        S, Td = market.S, self.Td
        nan = lambda: np.full((S, Td), np.nan)  # noqa: E731
        self.has_dec = np.zeros((S, Td), dtype=bool)
        self.close_dec = nan()
        self.raw = np.zeros((S, Td), dtype=bool)
        self.elig = np.zeros((S, Td), dtype=bool)
        self.mask_ok = np.ones((S, Td), dtype=bool)
        self.atr_d, self.atr_pct, self.adx_d = nan(), nan(), nan()
        self.regime = idata.align(market.btc_reg.astype(float), dec_index).fillna(0.0).to_numpy() > 0.5
        if self.family == 1:
            n_hours, self.m = spec.params
            self.N = fam.bars_in(spec.tf, n_hours)
            self.level = nan()
        elif self.family == 2:
            self.n, self.z_e = spec.params
            self.stop, self.target, self.rr, self.atr_tf, self.z = nan(), nan(), nan(), nan(), nan()
            self.adx_ok = np.zeros((S, Td), dtype=bool)
        else:
            self.L, self.K, self.b = spec.params
            self.rmat = nan()
        for i, c in enumerate(market.coins):
            df = frames[c]
            daily = market.data["daily"][c]
            if self.family == 1:
                sig = fam.family1_signals(df, daily, market.btc_reg, self.N, self.m)
            elif self.family == 2:
                sig = fam.family2_signals(df, daily, market.btc_reg, self.n, self.z_e)
            else:
                sig = fam.family1_signals(df, daily, market.btc_reg, 1)  # daily alignment only
                rc = df["close"] / df["close"].shift(self.L) - 1.0
                d = fam._daily_aligned(daily, df.index)
                warm = rc.notna().to_numpy() & idata.wilder_warm(d["n_daily"].to_numpy()) & d["atr_d"].notna().to_numpy()
                sig["eligible"] = idata.eligible(d["n_daily"].to_numpy(), warm)
                sig["r"] = rc
            g = sig.reindex(dec_index)
            present = g["close"].notna().to_numpy()
            self.has_dec[i] = present
            self.close_dec[i] = g["close"].to_numpy(float)
            self.raw[i] = present & g["raw_entry"].fillna(False).to_numpy(bool) if self.family != 3 else False
            self.elig[i] = present & g["eligible"].fillna(False).to_numpy(bool)
            self.atr_d[i] = g["atr_d"].to_numpy(float)
            self.atr_pct[i] = g["atr_pct"].to_numpy(float)
            self.adx_d[i] = g["adx_d"].to_numpy(float)
            if self.family == 1:
                self.level[i] = g["channel"].to_numpy(float)
            elif self.family == 2:
                for k in ("stop", "target", "rr", "atr_tf", "z"):
                    getattr(self, k)[i] = g[k].to_numpy(float)
                self.adx_ok[i] = g["adx_ok"].fillna(False).to_numpy(bool)
                if spec.tf == "15m":
                    self.mask_ok[i] = idata.entry_allowed_15m_mask(c, self.dec_ns, self.n, self.n)
            else:
                self.rmat[i] = g["r"].to_numpy(float)

    # -- entry candidates and family-3 rank state, for the real or a shifted raw signal ------- #
    def candidates(self, raw: np.ndarray | None = None, rmat: np.ndarray | None = None):
        """Returns (cand (S,Td) bool, order_key (S,Td) or None, exit_flag (S,Td) bool or None).
        `raw` / `rmat` override the real raw entry series / rank state (placebo landing)."""
        reg = self.regime[None, :]
        if self.family == 1:
            raw = self.raw if raw is None else raw
            cand = raw & self.has_dec & reg & self.elig
            return cand, None, self.has_dec & ~reg
        if self.family == 2:
            raw = self.raw if raw is None else raw
            cand = raw & self.has_dec & self.adx_ok & reg & self.elig & self.mask_ok
            return cand, None, None
        rm = self.rmat if rmat is None else rmat
        ranks = fam.family3_ranks(rm.T, (self.elig & self.has_dec).T, None).T  # (S, Td)
        in_target = (ranks >= 1) & (ranks <= self.K) & reg
        cand = in_target & self.has_dec & self.elig
        exit_flag = self.has_dec & (~reg | (ranks == 0) | (ranks > self.K + self.b))
        return cand, ranks, exit_flag

    def raw_blocked(self, raw: np.ndarray | None = None):
        """Signals that pass every gate except eligibility / the 15m mask (rejected-signal log)."""
        reg = self.regime[None, :]
        raw = self.raw if raw is None else raw
        if self.family == 1:
            base = raw & self.has_dec & reg
        elif self.family == 2:
            base = raw & self.has_dec & self.adx_ok & reg
        else:
            return None
        return base & ~(self.elig & self.mask_ok)


# --------------------------------------------------------------------------- #
# Costs
# --------------------------------------------------------------------------- #
class CostModel:
    """Per (coin, base bar) fee and slippage. Flat views broadcast a scalar; the tiered view has
    a per-bar slippage array built from DAILY quote volume (H5)."""

    def __init__(self, name: str, fee, slip):
        self.name = name
        self._fee, self._slip = fee, slip

    def fee(self, s: int, k: int) -> float:
        f = self._fee
        return float(f) if np.isscalar(f) else float(f[s, k])

    def slip(self, s: int, k: int) -> float:
        x = self._slip
        return float(x) if np.isscalar(x) else float(x[s, k])

    @classmethod
    def flat(cls, costs: Costs) -> "CostModel":
        return cls(costs.name, float(costs.fee), float(costs.slippage))


def daily_tier_volume(df_1h: pd.DataFrame) -> pd.Series:
    """Trailing 30-day median of DAILY quote volume, divided by 3, indexed by the day's CLOSE time
    (value known at that close). Daily quote volume is the registered definition (prereg §5.2, H5):
    the sum over the day's native 1h bars of (1h close x 1h volume), complete UTC days only
    (intraday_data.daily_quote_volume_from_1h) -- not the daily close times the day's volume."""
    qv = idata.daily_quote_volume_from_1h(df_1h)
    return qv.rolling(TIER_MEDIAN_DAYS, min_periods=1).median() / TIER_VOLUME_DIVISOR


def tiered_cost_model(market: Market, base: Costs = COST_PRESETS[GATING_COST]) -> CostModel:
    """Tier of base bar k (open on UTC day D) = tier of day D-1's trailing median / 3 (H5, AT13):
    a per-calendar-day series of 'median through day D' is shifted one day by
    tiered_slippage.tiered_costs_series_by_symbol, then mapped onto base bars by open date."""
    first_day = (market.start - pd.Timedelta(days=40)).normalize()
    days = pd.date_range(first_day, market.end, freq="1D", tz="UTC")  # day D = [D, D+1)
    open_day = (market.base_ns - market.step - first_day.value) // NS_DAY
    slip = np.empty((market.S, market.Tb))
    vols = {}
    for c in market.coins:
        med = daily_tier_volume(market.data["native_1h"][c])
        through_day = med.copy()
        through_day.index = med.index - pd.Timedelta(days=1)  # close at D+1 00:00 -> 'through day D'
        vols[c] = through_day.reindex(days, method="ffill").to_numpy(float)
    series = tiered_costs_series_by_symbol(vols, base)
    for i, c in enumerate(market.coins):
        per_day = np.array([x.slippage for x in series[c]])
        slip[i] = per_day[open_day]
    return CostModel(f"{base.name}_tiered", float(base.fee), slip)


def cost_views() -> dict[str, CostModel | str]:
    """The champion's cost-sensitivity rows (prereg §7 item 4). 'tiered' needs the market."""
    return {
        "paper_today_0.10%": CostModel.flat(COST_PRESETS["paper_today_0.10%"]),
        "kraken_T3_maker": CostModel.flat(COST_PRESETS["kraken_T3_maker"]),
        "kraken_T3_taker": CostModel.flat(COST_PRESETS["kraken_T3_taker"]),
        "kraken_T1_taker": CostModel.flat(COST_PRESETS["kraken_T1_taker"]),
        "kraken_T3_taker_+25%": CostModel.flat(STRESS_COSTS),
        "kraken_T3_taker_tiered": "tiered",
    }


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
TRADE_FIELDS = (
    "s", "d_signal", "fill_k", "exit_k", "exit_kind", "entry_px_raw", "entry_px", "qty", "quote",
    "planned_risk", "initial_stop", "target", "time_stop_k", "exit_px_raw", "exit_px", "reason",
    "final_stop", "fee_entry", "fee_exit", "slip_usd", "gross_pnl", "net_pnl", "R", "R_gross",
    "expo", "equity_signal", "entry_ref", "maker_filled", "entry_ns", "exit_ns",
)


@dataclass
class RunResult:
    spec: fam.SetSpec
    cost_name: str
    exit_model: str
    fill_model: str
    start_equity: float
    trades: list = field(default_factory=list)          # tuples in TRADE_FIELDS order (+ level)
    light: list = field(default_factory=list)           # collect=False: (s, d, fill_k, exit_k, net, R, entry_ns, exit_ns)
    rejections: list = field(default_factory=list)      # (s, d, reason, mask_blocked, ineligible)
    missed: list = field(default_factory=list)          # maker misses: (s, d, limit)
    checks: int = 0
    approvals: int = 0
    reject_reasons: dict = field(default_factory=dict)
    equity: np.ndarray | None = None                    # on base closes
    exposure: np.ndarray | None = None
    final_equity: float = float("nan")
    mask_violations: int = 0

    def col(self, name: str) -> np.ndarray:
        i = TRADE_FIELDS.index(name)
        return np.array([t[i] for t in self.trades])

    def core(self) -> dict:
        """The columns every statistic needs, from either collection mode."""
        if self.trades:
            fi = TRADE_FIELDS.index
            rows = [(t[fi("s")], t[fi("net_pnl")], t[fi("R")], t[fi("entry_ns")], t[fi("exit_ns")]) for t in self.trades]
        else:
            rows = [(t[0], t[4], t[5], t[6], t[7]) for t in self.light]
        if not rows:
            z = np.zeros(0)
            return dict(s=z.astype(int), net=z, R=z, entry_ns=z.astype(np.int64), exit_ns=z.astype(np.int64))
        a = list(zip(*rows))
        return dict(s=np.array(a[0], dtype=int), net=np.array(a[1], float), R=np.array(a[2], float),
                    entry_ns=np.array(a[3], dtype=np.int64), exit_ns=np.array(a[4], dtype=np.int64))


def _reason_class(reason: str) -> str:
    r = reason.split(";")[0]
    if r.startswith("no room left"):
        return "no_room"
    if r.startswith("new-entry rate cap"):
        return "rate_cap"
    if r.startswith("market volatility too high"):
        return "volatility"
    if r.startswith("risk/reward"):
        return "risk_reward"
    if r.startswith("cooldown active"):
        return "cooldown"
    if r.startswith("daily loss limit"):
        return "daily_loss"
    if r.startswith("sized order"):
        return "min_order"
    if r.startswith("invalid risk input"):
        return "invalid_input"
    return "other"


# --------------------------------------------------------------------------- #
# The simulator
# --------------------------------------------------------------------------- #
class Simulator:
    def __init__(self, market: Market, sd: SetData, costs: CostModel, risk_cfg: dict,
                 exit_model: str = "close", fill_model: str = "taker"):
        if exit_model not in ("close", "touch"):
            raise ValueError(exit_model)
        if fill_model not in ("taker", "maker"):
            raise ValueError(fill_model)
        if fill_model == "maker" and exit_model != "close":
            raise ValueError("the maker model runs with the gating close-trigger exits")
        self.m, self.sd, self.costs, self.risk_cfg = market, sd, costs, risk_cfg
        self.exit_model, self.fill_model = exit_model, fill_model
        self.touch = exit_model == "touch"
        self.r = sd.r
        self.taker = costs  # exits are always taker-style at this cost view

    # ---------------------------------------------------------------- exits -------------- #
    def _exec_after(self, s: int, k: int):
        """Close-trigger at base close k -> (exec index, raw price, is_end)."""
        m = self.m
        e = int(m.next_bar[s, k]) if k < m.Tb else m.Tb
        if e >= m.Tb:
            kl = int(m.last_bar[s])
            return kl, float(m.C[s, kl]), True
        return e, float(m.O[s, e]), False

    def _exit_f13(self, s: int, j: int, stop0: float, trail_m: float | None, flag: np.ndarray):
        """Families 1 and 3. Stop checked on every base close (touch: every base bar) from the fill
        bar j; trailing (family 1) updated after each decision close d >= d_first; `flag` (S-row,
        decision axis) is the regime exit (F1) or the rank/regime exit (F3), a close-trigger at
        decision closes d >= d_first. Returns a dict describing the exit."""
        m, sd, r, touch = self.m, self.sd, self.r, self.touch
        C, L, O = m.C[s], m.L[s], m.O[s]
        Tb, Td = m.Tb, sd.Td
        d_first = -(-j // r)
        # segment before the first decision close at/after the fill: stop0 governs
        k_end = min(d_first * r, Tb - 1)
        seg = L[j:k_end + 1] <= stop0 if touch else C[j:k_end + 1] <= stop0
        if seg.any():
            return self._stop_hit(s, j + int(np.argmax(seg)), stop0, stop0)
        cur, hc = stop0, -np.inf
        dclose, datr = sd.close_dec[s], sd.atr_d[s]
        d_a, w = d_first, 64
        while d_a < Td:
            d_b = min(Td, d_a + w)
            if trail_m is not None:
                cd = dclose[d_a:d_b]
                hcs = np.fmax.accumulate(np.concatenate(([hc], cd)))[1:]
                cand = hcs - trail_m * datr[d_a:d_b]
                cand = np.where(np.isnan(cd) | np.isnan(cand), -np.inf, cand)
                Sd = np.maximum.accumulate(np.maximum(cand, cur))
            else:
                hcs = None
                Sd = np.full(d_b - d_a, cur)
            k0, k1 = d_a * r + 1, min(d_b * r, Tb - 1)
            ks = None
            if k0 <= k1:
                stop_eff = np.repeat(Sd, r)[: k1 - k0 + 1]
                hit = (L[k0:k1 + 1] <= stop_eff) if touch else (C[k0:k1 + 1] <= stop_eff)
                if hit.any():
                    off = int(np.argmax(hit))
                    ks, ks_stop = k0 + off, float(stop_eff[off])
            fl = flag[d_a:d_b]
            kf = d_a * r + int(np.argmax(fl)) * r if fl.any() else None
            if ks is not None or kf is not None:
                if kf is None or (ks is not None and ks <= kf):
                    # close model: same close -> stop reason wins; touch: intrabar touch first
                    return self._stop_hit(s, ks, ks_stop, stop0)
                stop_at = float(Sd[(kf // r) - d_a - 1]) if (kf // r) - 1 >= d_a else cur
                return self._flag_hit(s, kf, stop_at)
            cur = float(Sd[-1])
            if hcs is not None:
                hc = float(hcs[-1])
            d_a, w = d_b, w * 2
        return self._end(s, cur)

    def _stop_hit(self, s, k, stop_level, stop0):
        trail = stop_level > stop0 * (1 + 1e-12)
        if self.touch:
            o = float(self.m.O[s, k])
            if o <= stop_level:
                return dict(exit_k=k, px=o, reason="stop_gap", final_stop=stop_level, kind="touch")
            return dict(exit_k=k, px=stop_level, reason="trail" if trail else "stop", final_stop=stop_level, kind="touch")
        e, px, end = self._exec_after(s, k)
        return dict(exit_k=e, px=px, reason="end" if end else ("trail" if trail else "stop"),
                    final_stop=stop_level, kind="close" if not end else "end", trigger_k=k)

    def _flag_hit(self, s, k, stop_level):
        e, px, end = self._exec_after(s, k)
        reason = ("regime" if self.sd.family == 1 else "rank")
        if self.sd.family == 3 and not self.sd.regime[k // self.r]:
            reason = "regime"
        return dict(exit_k=e, px=px, reason="end" if end else reason, final_stop=stop_level,
                    kind="close" if not end else "end", trigger_k=k)

    def _end(self, s, stop_level):
        kl = int(self.m.last_bar[s])
        return dict(exit_k=kl, px=float(self.m.C[s, kl]), reason="end", final_stop=stop_level, kind="end")

    def _exit_f2(self, s: int, j: int, stop: float, target: float, maker_target: bool):
        m, touch = self.m, self.touch
        C, L, H, O = m.C[s], m.L[s], m.H[s], m.O[s]
        bars = m.bar_idx[s]
        p = int(np.searchsorted(bars, j))
        n = int(self.sd.n)
        k_time = int(bars[p + n - 1]) if p + n - 1 < len(bars) else None
        hi = k_time if k_time is not None else m.Tb - 1
        seg = slice(j, hi + 1)
        if touch:
            st = L[seg] <= stop
            tg = H[seg] >= target
            hit = st | tg
            if hit.any():
                off = int(np.argmax(hit))
                k = j + off
                o = float(O[k])
                if st[off]:
                    px = o if o <= stop else stop
                    return dict(exit_k=k, px=px, reason="stop_gap" if o <= stop else "stop",
                                final_stop=stop, kind="touch", time_k=k_time)
                return dict(exit_k=k, px=max(o, target), reason="target", final_stop=stop, kind="touch", time_k=k_time)
        else:
            st = C[seg] <= stop
            if maker_target:
                tg = H[seg] >= target * (1 + MAKER_TRADE_THROUGH)
                tg[0] = False  # never on the maker entry-fill bar (intrabar order unknown)
                ks = j + int(np.argmax(st)) if st.any() else None
                kt = j + int(np.argmax(tg)) if tg.any() else None
                if kt is not None and (ks is None or kt <= ks):
                    return dict(exit_k=kt, px=float(target), reason="target", final_stop=stop,
                                kind="maker_target", time_k=k_time)
                if ks is not None:
                    e, px, end = self._exec_after(s, ks)
                    return dict(exit_k=e, px=px, reason="end" if end else "stop", final_stop=stop,
                                kind="close" if not end else "end", trigger_k=ks, time_k=k_time)
            else:
                tg = C[seg] >= target
                hit = st | tg
                if hit.any():
                    off = int(np.argmax(hit))
                    e, px, end = self._exec_after(s, j + off)
                    reason = "stop" if st[off] else "target"
                    return dict(exit_k=e, px=px, reason="end" if end else reason, final_stop=stop,
                                kind="close" if not end else "end", trigger_k=j + off, time_k=k_time)
        if k_time is not None:
            e, px, end = self._exec_after(s, k_time)
            return dict(exit_k=e, px=px, reason="end" if end else "time", final_stop=stop,
                        kind="close" if not end else "end", trigger_k=k_time, time_k=k_time)
        out = self._end(s, stop)
        out["time_k"] = None
        return out

    # ---------------------------------------------------------------- main loop ---------- #
    def run(self, raw: np.ndarray | None = None, rmat: np.ndarray | None = None, collect: bool = True,
            log_rejections: bool = False) -> RunResult:
        m, sd, r = self.m, self.sd, self.r
        fam_no = sd.family
        cand, ranks, exit_flag = sd.candidates(raw, rmat)
        blocked = sd.raw_blocked(raw) if log_rejections else None
        res = RunResult(sd.spec, self.costs.name, self.exit_model, self.fill_model,
                        float(self.risk_cfg["starting_cash_usd"]))
        risk = make_risk_engine(self.risk_cfg, m.coins)

        # candidate (d, s) pairs in processing order
        any_mask = cand if blocked is None else (cand | blocked)
        ss, dd = np.nonzero(any_mask)
        if fam_no == 3:
            key = ranks[ss, dd]
            order = np.lexsort((key, dd))
        else:
            order = np.lexsort((ss, dd))
        ss, dd = ss[order], dd[order]

        cash = res.start_equity
        busy_until = np.zeros(m.S, dtype=np.int64)       # coin is flat at decisions with bi >= this
        open_pos: dict[int, list] = {}                   # s -> [qty, fill_k, exit_k, proceeds]
        events: list = []                                # (k, kind, s, amount) ; kind 0 fill, 1 exit, 2 release
        pending_commit: dict[int, tuple[int, float]] = {}  # s -> (resolve_k, quote) not yet executed
        import heapq

        dec_dt = None
        trades = res.trades
        fee_maker = MAKER_FEE
        n_pairs = len(ss)
        p = 0
        while p < n_pairs:
            d = int(dd[p])
            q = p
            while q < n_pairs and dd[q] == d:
                q += 1
            bi = d * r
            # 1) execute fills / exits / releases up to and including base bar bi
            while events and events[0][0] <= bi:
                k_ev, kind, s_ev, amt = heapq.heappop(events)
                if kind == 0:
                    cash -= amt
                    pending_commit.pop(s_ev, None)
                    open_pos[s_ev][4] = True
                elif kind == 1:
                    cash += amt
                    open_pos.pop(s_ev, None)
                else:
                    pending_commit.pop(s_ev, None)
            # 2) equity at this decision close (cash + open positions marked at their last close)
            mtm = 0.0
            for s_o, pos in open_pos.items():
                if pos[4]:
                    mtm += pos[0] * m.Cff[s_o, bi]
            equity = cash + mtm
            committed = sum(v[1] for v in pending_commit.values())
            now = None
            for idx_pair in range(p, q):
                s = int(ss[idx_pair])
                if busy_until[s] > bi:
                    continue
                if not cand[s, d]:
                    res.rejections.append((s, d, "blocked", bool(not sd.mask_ok[s, d]), bool(not sd.elig[s, d])))
                    continue
                if now is None:
                    now = pd.Timestamp(int(sd.dec_ns[d]), tz="UTC").to_pydatetime()
                close_t = float(sd.close_dec[s, d])
                if fam_no == 1:
                    stop0 = close_t - sd.m * float(sd.atr_d[s, d])
                    target, rr = float("nan"), None
                elif fam_no == 2:
                    stop0, target = float(sd.stop[s, d]), float(sd.target[s, d])
                    rr = float(sd.rr[s, d])
                else:
                    stop0 = close_t - fam.F3_STOP_ATR_D_MULT * float(sd.atr_d[s, d])
                    target, rr = float("nan"), None
                atr_pct = float(sd.atr_pct[s, d])
                res.checks += 1
                dec = risk.check_new_trade(
                    m.coins[s], "buy", portfolio_equity=equity, cash_available=cash - committed,
                    entry_price=close_t, stop_loss=stop0, risk_reward=rr,
                    open_positions_value=mtm + committed, atr_pct=atr_pct, now=now,
                )
                if not dec.allowed:
                    cls = _reason_class(dec.reason)
                    res.reject_reasons[cls] = res.reject_reasons.get(cls, 0) + 1
                    if log_rejections:
                        res.rejections.append((s, d, dec.reason.split(";")[0], False, False))
                    continue
                res.approvals += 1
                risk.record_trade(m.coins[s], now=now)
                quote = float(dec.max_quote_amount)
                expo = (mtm + committed + quote) / equity if equity > 0 else 0.0
                # -- the fill --
                if self.fill_model == "taker":
                    j = int(m.next_bar[s, bi])
                    if j >= m.Tb:
                        continue  # no base bar left in the window: the order never executes
                    raw_px = float(m.O[s, j])
                    slip = self.costs.slip(s, j)
                    fee_rate = self.costs.fee(s, j)
                    fill_px = raw_px * (1.0 + slip)
                    maker_filled = False
                else:
                    limit = close_t
                    lo, hi_k = bi + 1, min(bi + r, m.Tb - 1)
                    seg = m.L[s, lo:hi_k + 1] <= limit * (1.0 - MAKER_TRADE_THROUGH)
                    if not seg.any():
                        busy_until[s] = bi + r
                        pending_commit[s] = (bi + r, quote)
                        committed += quote
                        heapq.heappush(events, (bi + r, 2, s, 0.0))
                        res.missed.append((s, d, limit))
                        continue
                    j = lo + int(np.argmax(seg))
                    raw_px = fill_px = limit
                    slip, fee_rate = 0.0, fee_maker
                    maker_filled = True
                notional = quote / (1.0 + fee_rate)
                fee_e = notional * fee_rate
                qty = notional / fill_px
                # -- the exit (pure function of this coin's data from the fill on) --
                if fam_no == 1:
                    ex = self._exit_f13(s, j, stop0, sd.m, exit_flag[s])
                elif fam_no == 3:
                    ex = self._exit_f13(s, j, stop0, None, exit_flag[s])
                else:
                    ex = self._exit_f2(s, j, stop0, target, maker_target=self.fill_model == "maker")
                k_x = int(ex["exit_k"])
                if ex["kind"] == "maker_target":
                    x_slip, x_fee = 0.0, fee_maker
                else:
                    x_slip, x_fee = self.costs.slip(s, k_x), self.costs.fee(s, k_x)
                x_px = ex["px"] * (1.0 - x_slip)
                x_notional = qty * x_px
                fee_x = x_notional * x_fee
                proceeds = x_notional - fee_x
                net = proceeds - quote
                planned = qty * (close_t - stop0)
                R = net / planned if planned > 0 else float("nan")
                R_gross = (ex["px"] - raw_px) / (close_t - stop0) if close_t > stop0 else float("nan")
                # flat again from the decision close of the exit bar: a close-trigger exit executes
                # at the open of k_x, a touch / maker-target exit inside bar k_x; 'end' never frees.
                busy_until[s] = m.Tb + 1 if ex["kind"] == "end" else k_x
                pending_commit[s] = (j, quote)
                committed += quote
                open_pos[s] = [qty, j, k_x, proceeds, False]
                heapq.heappush(events, (j, 0, s, quote))
                # exits executed at an open (close model) or inside a bar (touch, maker target) or at
                # the final close (end): booked at that base index; a fill and an exit of the same
                # coin never share an index because exit_k >= fill bar and fills are popped first.
                heapq.heappush(events, (k_x, 1, s, proceeds))
                if collect:
                    if fam_no == 3 and ranks is not None:
                        level = float(ranks[s, d])
                    elif fam_no == 2:
                        level = float(sd.z[s, d])
                    else:
                        level = float(sd.level[s, d])
                    slip_usd = qty * (fill_px - raw_px) + qty * (ex["px"] - x_px)
                    entry_ns = int(m.base_ns[j] - m.step)
                    exit_ns = int(m.base_ns[k_x]) if ex["kind"] == "end" else int(m.base_ns[k_x] - m.step)
                    trades.append((
                        s, d, j, k_x, ex["kind"], raw_px, fill_px, qty, quote, planned, stop0, target,
                        ex.get("time_k"), ex["px"], x_px, ex["reason"], ex["final_stop"], fee_e, fee_x,
                        slip_usd, qty * (ex["px"] - raw_px), net, R, R_gross, expo, equity, close_t,
                        maker_filled, entry_ns, exit_ns, level,
                    ))
                else:
                    res.light.append((s, d, j, k_x, net, R, int(m.base_ns[j] - m.step),
                                      int(m.base_ns[k_x]) if ex["kind"] == "end" else int(m.base_ns[k_x] - m.step)))
            p = q
        # drain the remaining events
        while events:
            k_ev, kind, s_ev, amt = heapq.heappop(events)
            if kind == 0:
                cash -= amt
            elif kind == 1:
                cash += amt
        res.final_equity = cash
        if collect:
            self._equity_series(res)
        return res

    def _equity_series(self, res: RunResult) -> None:
        m = self.m
        delta = np.zeros(m.Tb + 1)
        inv = np.zeros(m.Tb)
        fi = TRADE_FIELDS.index
        for t in res.trades:
            s, j, kx, qty, quote, proceeds_kind = t[fi("s")], t[fi("fill_k")], t[fi("exit_k")], t[fi("qty")], t[fi("quote")], t[fi("exit_kind")]
            proceeds = t[fi("net_pnl")] + quote
            delta[j] -= quote
            delta[kx] += proceeds
            # held at closes j .. kx-1 (close model: sold at the open of kx; touch / maker target:
            # sold inside bar kx; end: sold at the close of kx, after the mark)
            inv[j:kx] += qty * m.Cff[s, j:kx]
        cash = res.start_equity + np.cumsum(delta[:-1])
        eq = cash + inv
        res.equity = eq
        with np.errstate(divide="ignore", invalid="ignore"):
            res.exposure = np.where(eq > 0, inv / eq, 0.0)


# --------------------------------------------------------------------------- #
# Convenience wrappers
# --------------------------------------------------------------------------- #
def simulate(market: Market, sd: SetData, costs: CostModel, risk_cfg: dict, exit_model: str = "close",
             fill_model: str = "taker", raw: np.ndarray | None = None, rmat: np.ndarray | None = None,
             collect: bool = True, log_rejections: bool = False) -> RunResult:
    return Simulator(market, sd, costs, risk_cfg, exit_model, fill_model).run(raw, rmat, collect, log_rejections)


def simulate_frames(family: int, params: tuple, universe: tuple, base_frames: dict, decision_frames: dict,
                    daily: dict, costs_spec: Costs | CostModel, exit_model: str = "close",
                    fill_model: str = "taker", signals_override: np.ndarray | None = None,
                    start: pd.Timestamp = idata.B_START, days: int = idata.B_DAYS, risk_cfg: dict | None = None,
                    tf: str | None = None) -> RunResult:
    """The H4 signature: builds the market and set arrays from frames, then runs `simulate`.
    `decision_frames` must be the frames the set's decision timeframe implies (they are re-derived
    and compared, so a mismatched or mixed-source frame raises)."""
    base_tf = next(iter(base_frames.values())).attrs.get("source", "native_1h").replace("native_", "")
    spec = next(s for s in fam.GRIDS[family] if tuple(s.params) == tuple(params) and (tf is None or s.tf == tf))
    native_1h = {c: (base_frames[c] if base_tf == "1h" else None) for c in base_frames}
    data = dict(tf=base_tf, universe=tuple(universe), native_1h=native_1h, base=base_frames, daily=daily)
    idata.assert_sources(base_tf, base_frames, decision_frames, list(daily.values()))
    market = Market(data, start, days, coins=universe)
    sd = SetData(market, spec)
    cm = costs_spec if isinstance(costs_spec, CostModel) else CostModel.flat(costs_spec)
    raw = signals_override if family != 3 else None
    rmat = signals_override if family == 3 else None
    return simulate(market, sd, cm, risk_cfg or load_risk_cfg(), exit_model, fill_model, raw, rmat)


def trades_frame(res: RunResult, market: Market, sd: SetData, universe_name: str, placebo_draw=None) -> pd.DataFrame:
    """The H8 per-trade log for a collected run (statistics columns period/fold/entry_block are
    filled by intraday_stats.annotate)."""
    cols = list(TRADE_FIELDS) + ["level"]
    df = pd.DataFrame(res.trades, columns=cols)
    spec = sd.spec
    out = pd.DataFrame({
        "family": spec.family, "set_id": spec.set_id, "universe": universe_name, "cost_view": res.cost_name,
        "exit_model": res.exit_model, "fill_model": res.fill_model,
        "symbol": [market.coins[s] for s in df["s"]] if len(df) else [],
        "decision_tf": spec.tf, "base_tf": spec.base_tf,
    }, index=df.index)
    if not len(df):
        return pd.DataFrame(columns=H8_COLUMNS)
    to_ts = lambda ns: pd.to_datetime(ns, utc=True)  # noqa: E731
    out["signal_time"] = to_ts(sd.dec_ns[df["d_signal"].to_numpy(int)])
    out["entry_time"] = to_ts(df["entry_ns"].to_numpy(np.int64))
    out["entry_px_raw"], out["entry_px"], out["qty"] = df["entry_px_raw"], df["entry_px"], df["qty"]
    out["approved_quote"], out["planned_risk_usd"] = df["quote"], df["planned_risk"]
    out["initial_stop"], out["target"] = df["initial_stop"], df["target"]
    tk = df["time_stop_k"]
    out["time_stop_at"] = [to_ts(int(market.base_ns[int(k)])) if k is not None and not pd.isna(k) else pd.NaT for k in tk]
    out["exit_time"] = to_ts(df["exit_ns"].to_numpy(np.int64))
    out["exit_px_raw"], out["exit_px"], out["exit_reason"] = df["exit_px_raw"], df["exit_px"], df["reason"]
    out["final_stop"] = df["final_stop"]
    out["bars_held_base"] = df["exit_k"] - df["fill_k"]
    out["hours_held"] = (df["exit_ns"] - df["entry_ns"]) / 3.6e12
    out["fee_entry"], out["fee_exit"], out["slippage_usd"] = df["fee_entry"], df["fee_exit"], df["slip_usd"]
    out["gross_pnl"], out["net_pnl"], out["R"], out["R_gross"] = df["gross_pnl"], df["net_pnl"], df["R"], df["R_gross"]
    out["expo_at_entry"], out["equity_at_signal"] = df["expo"], df["equity_signal"]
    ds, ss = df["d_signal"].to_numpy(int), df["s"].to_numpy(int)
    out["regime_on"] = sd.regime[ds]
    out["adx_d"] = sd.adx_d[ss, ds]
    out["atr_d"] = sd.atr_d[ss, ds]
    out["atr_tf"] = sd.atr_tf[ss, ds] if sd.family == 2 else np.nan
    out["z_or_rank_or_breakout_level"] = df["level"]
    out["rr_at_signal"] = sd.rr[ss, ds] if sd.family == 2 else np.nan
    out["period"] = ""
    out["fold"] = ""
    out["entry_day"] = out["entry_time"].dt.strftime("%Y-%m-%d")
    out["entry_block"] = -1
    out["maker_filled"] = df["maker_filled"]
    out["placebo_draw"] = "" if placebo_draw is None else placebo_draw
    return out[H8_COLUMNS]


H8_COLUMNS = [
    "family", "set_id", "universe", "cost_view", "exit_model", "fill_model", "symbol", "decision_tf", "base_tf",
    "signal_time", "entry_time", "entry_px_raw", "entry_px", "qty", "approved_quote", "planned_risk_usd",
    "initial_stop", "target", "time_stop_at", "exit_time", "exit_px_raw", "exit_px", "exit_reason", "final_stop",
    "bars_held_base", "hours_held", "fee_entry", "fee_exit", "slippage_usd", "gross_pnl", "net_pnl", "R", "R_gross",
    "expo_at_entry", "equity_at_signal", "regime_on", "adx_d", "atr_d", "atr_tf", "z_or_rank_or_breakout_level",
    "rr_at_signal", "period", "fold", "entry_day", "entry_block", "maker_filled", "placebo_draw",
]
REJECTED_COLUMNS = ["family", "set_id", "universe", "symbol", "signal_time", "reason", "mask_blocked", "ineligible"]


def rejections_frame(res: RunResult, market: Market, sd: SetData, universe_name: str) -> pd.DataFrame:
    rows = [
        (sd.family, sd.spec.set_id, universe_name, market.coins[s], pd.Timestamp(int(sd.dec_ns[d]), tz="UTC"),
         reason, mb, ie)
        for s, d, reason, mb, ie in res.rejections
    ]
    return pd.DataFrame(rows, columns=REJECTED_COLUMNS)
