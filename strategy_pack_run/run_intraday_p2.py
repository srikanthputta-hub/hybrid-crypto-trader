"""run_intraday_p2.py - executes the registered P2 evaluation of docs/intraday_prereg.md end to end
for one family (or all three) and writes the machine-readable results JSON, the per-family report
and the gitignored per-trade / rejected-signal logs (docs/intraday_harness_changes.md H9).

    python strategy_pack_run/run_intraday_p2.py --family 1 --workers 4
    python strategy_pack_run/run_intraday_prereg.py --workers 4          # all three, H9 entry point

No flag can change a parameter, gate, window, seed, cost or universe: they are module constants
copied from the prereg (intraday_data / intraday_families / intraday_stats / intraday_runner) and
asserted by test_intraday_constants.py. Flags exist only for --family (which part of the registered
work to run), --workers, the data root (input path) and output paths.

THE REGISTERED EVALUATION IS RUN EXACTLY ONCE, BY P2 (prereg §0 rule 1). During the P1 build this
module is exercised only through `evaluate_family(..., provider=SyntheticProvider(...))` on
synthetic data (test_intraday_runner_p2.py, validate_intraday_synthetic.py).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import intraday_data as idata  # noqa: E402
import intraday_families as fam  # noqa: E402
import intraday_runner as ir  # noqa: E402
import intraday_stats as st  # noqa: E402

UNIVERSES = {
    "B": dict(start=idata.B_START, days=idata.B_DAYS, coins={"1h": idata.UNIVERSE_B, "15m": idata.UNIVERSE_B_15M}),
    "A": dict(start=idata.A_START, days=idata.A_DAYS, coins={"1h": idata.UNIVERSE_A, "15m": idata.UNIVERSE_A_15M}),
}
CORE_15M = ("BTC", "ETH", "SOL", "LTC")
DUST_USD = 50.0
DEFAULT_JSON = _THIS_DIR / "intraday_p2_results.json"
DEFAULT_DOCS = _REPO_ROOT / "docs"
DEFAULT_LOGS = _THIS_DIR / "intraday_p2_logs"


# --------------------------------------------------------------------------- #
# Data providers
# --------------------------------------------------------------------------- #
class RealProvider:
    """The frozen snapshot. Verifies the prefix hash of every file a family reads before loading."""

    kind = "real"

    def __init__(self, root=None):
        self.root = idata.data_root(root)
        self._cache: dict = {}
        self.hashes: dict = {}

    def verify(self, family: int) -> dict:
        rels = set()
        for u in UNIVERSES.values():
            for tf in ({"1h"} if family != 2 else {"1h", "15m"}):
                rels |= set(idata.files_for(u["coins"][tf], tf))
        got = idata.verify_snapshot(self.root, files=sorted(rels))
        self.hashes.update(got)
        return got

    def data(self, universe: str, base_tf: str) -> dict:
        key = (universe, base_tf)
        if key not in self._cache:
            self._cache[key] = idata.load_universe(UNIVERSES[universe]["coins"][base_tf], base_tf, self.root)
        return self._cache[key]


class SyntheticProvider:
    """Synthetic data in the same shape, on the same frozen windows. `factory(universe, base_tf)`
    returns a data dict (intraday_synthetic.make_data shape)."""

    kind = "synthetic"

    def __init__(self, factory, coins: dict | None = None):
        self.factory = factory
        self.coins = coins  # optional {"B": {"1h": (...), "15m": (...)}, "A": {...}} for cheap synthetic universes
        self._cache: dict = {}
        self.hashes: dict = {}

    def verify(self, family: int) -> dict:
        return {}

    def data(self, universe: str, base_tf: str) -> dict:
        key = (universe, base_tf)
        if key not in self._cache:
            self._cache[key] = self.factory(universe, base_tf)
        return self._cache[key]


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #
def _f(x):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(x)


def light_metrics(res: ir.RunResult, cut: pd.Timestamp) -> dict:
    core = res.core()
    R, e, x = core["R"], core["entry_ns"], core["exit_ns"]
    fin = np.isfinite(R)
    R, e, x = R[fin], e[fin], x[fin]
    is_m, oos, _ = st.purged_split(e, x, cut)
    return dict(n=int(len(R)), avgR=float(R.mean()) if len(R) else float("nan"),
                total_return=float(res.final_equity / res.start_equity - 1.0),
                is_n=int(is_m.sum()), is_avgR=float(R[is_m].mean()) if is_m.any() else float("nan"))


def run_summary(res: ir.RunResult, market: ir.Market, sd: ir.SetData, cut: pd.Timestamp, boot: bool = True) -> dict:
    tr = pd.DataFrame(res.trades, columns=list(ir.TRADE_FIELDS) + ["level"])
    tr = tr[np.isfinite(tr["R"].astype(float))] if len(tr) else tr
    n = len(tr)
    eq = res.equity
    daily_eq = eq[:: market.bpd]
    out = dict(set_id=sd.spec.set_id, set_no=sd.spec.set_no, cost=res.cost_name, exit_model=res.exit_model,
               fill_model=res.fill_model, trades=n, total_return=float(res.final_equity / res.start_equity - 1),
               final_equity=float(res.final_equity), checks=res.checks, approvals=res.approvals,
               reject_reasons=dict(sorted(res.reject_reasons.items())), missed_orders=len(res.missed),
               sharpe_daily=st.daily_sharpe(daily_eq), max_drawdown=st.max_drawdown(eq)[0],
               exposure=float(np.mean(res.exposure)))
    if n == 0:
        out.update(avgR=float("nan"), is_n=0, is_avgR=float("nan"), oos_n=0, oos_avgR=float("nan"), n_blocks=0)
        return out
    R = tr["R"].to_numpy(float)
    e, x = tr["entry_ns"].to_numpy(np.int64), tr["exit_ns"].to_numpy(np.int64)
    is_m, oos, strad = st.purged_split(e, x, cut)
    hold_days = (x - e) / st.NS_DAY
    wins, losses = tr.loc[tr.net_pnl > 0, "net_pnl"].sum(), -tr.loc[tr.net_pnl <= 0, "net_pnl"].sum()
    years = market.days / 365.0
    traded = float((tr["quote"] + tr["qty"] * tr["exit_px"]).sum())
    out.update(
        win_rate=float((tr["net_pnl"] > 0).mean()), avgR=float(R.mean()), avgR_gross=float(tr["R_gross"].mean()),
        cost_drag_R=float(tr["R_gross"].mean() - R.mean()),
        profit_factor=float(wins / losses) if losses > 0 else float("inf"),
        hold_days_median=float(np.median(hold_days)), hold_days_mean=float(np.mean(hold_days)),
        is_n=int(is_m.sum()), is_avgR=float(R[is_m].mean()) if is_m.any() else float("nan"),
        oos_n=int(oos.sum()), oos_avgR=float(R[oos].mean()) if oos.any() else float("nan"), straddle_n=int(strad.sum()),
        turnover_annual=traded / float(np.mean(eq)) / years, trades_per_week=n / (market.days / 7.0),
        fees_pct_start=float((tr["fee_entry"] + tr["fee_exit"]).sum() / res.start_equity),
        dust_share=float((tr["quote"] < DUST_USD).mean()),
        median_stop_pct=float(np.median((tr["entry_ref"] - tr["initial_stop"]) / tr["entry_ref"])),
        gross_pct_per_trade=float(np.mean(tr["exit_px_raw"] / tr["entry_px_raw"] - 1)),
        gross_pct_per_day=float(np.sum(tr["exit_px_raw"] / tr["entry_px_raw"] - 1) / max(np.sum(hold_days), 1e-12)),
        positive_R_coins=int(sum(1 for _, g in tr.groupby("s") if g["R"].mean() > 0)),
        nan_R_trades=int(len(res.trades) - n),
    )
    Lb = st.block_days_for(e, x)
    out["block_days"] = Lb
    out["n_blocks"] = int(len(np.unique(st.block_ids(e, market.start, Lb))))
    if boot:
        b = st.block_bootstrap(R, e, market.start, Lb)
        out["boot"] = dict(pct={str(k): v for k, v in b["pct"].items()}, DE=b["DE"], n_eff=b["n_eff"],
                           var_block=b["var_block"], var_iid=b["var_iid"], per_block_max=b["per_block_max"],
                           per_block_median=b["per_block_median"])
    return out


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def code_commit() -> str:
    try:
        sha = subprocess.run(["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(_REPO_ROOT), "status", "--porcelain", "--", "strategy_pack_run", "src", "config"],
                               capture_output=True, text=True).stdout.strip()
        return sha + ("+dirty" if dirty else "")
    except Exception:  # pragma: no cover
        return "unknown"


# --------------------------------------------------------------------------- #
# Placebo workers (fork-shared state; every draw is a pure function of (d_i, family, set, data))
# --------------------------------------------------------------------------- #
_PL: dict = {}


def _placebo_task(task):
    draw_i, d, set_i = task
    market, sd = _PL["markets"][set_i], _PL["sds"][set_i]
    k = d * sd.bpd_dec
    if sd.family == 3:
        res = ir.simulate(market, sd, _PL["costs"], _PL["risk"], rmat=np.roll(sd.rmat, k, axis=1), collect=False)
    else:
        res = ir.simulate(market, sd, _PL["costs"], _PL["risk"], raw=st.shift_raw(sd.raw, d, sd.bpd_dec), collect=False)
    return draw_i, set_i, light_metrics(res, _PL["cut"])


def run_placebo(markets: list, sds: list, costs, risk_cfg, cut, shifts: list[int], workers: int = 1,
                order_seed: int | None = None) -> list[list[dict]]:
    """out[draw][set_index] = light metrics. Identical for any `workers` and any completion /
    submission order (`order_seed` shuffles submission, for AT15)."""
    _PL.update(markets=markets, sds=sds, costs=costs, risk=risk_cfg, cut=cut)
    tasks = [(i, d, j) for i, d in enumerate(shifts) for j in range(len(sds))]
    if order_seed is not None:
        random.Random(order_seed).shuffle(tasks)
    out = [[None] * len(sds) for _ in shifts]
    if workers <= 1:
        for t in tasks:
            i, j, m = _placebo_task(t)
            out[i][j] = m
    else:
        import multiprocessing as mp

        with mp.get_context("fork").Pool(workers) as pool:
            for i, j, m in pool.imap_unordered(_placebo_task, tasks, chunksize=1):
                out[i][j] = m
    return out


def draw_champions(placebo: list[list[dict]], specs: list) -> list[dict | None]:
    out = []
    for row in placebo:
        rows = [dict(set_no=specs[j].set_no, is_n=m["is_n"], is_avgR=m["is_avgR"]) for j, m in enumerate(row)]
        ch = st.champion(rows)
        if ch is None:
            out.append(None)
        else:
            j = [s.set_no for s in specs].index(ch["set_no"])
            out.append(dict(set_no=ch["set_no"], avgR=row[j]["avgR"], total_return=row[j]["total_return"]))
    return out


# --------------------------------------------------------------------------- #
# Evaluation of one family
# --------------------------------------------------------------------------- #
def evaluate_family(family: int, provider, workers: int = 1, logs_dir: Path | None = None,
                    risk_cfg: dict | None = None, n_placebo: int = st.N_PLACEBO, verbose: bool = True) -> dict:
    """The registered sequence for one family (H9): snapshot check -> 12 sets x {B, A} at
    kraken_T3_taker -> champion on B (purged IS) -> champion cost / exit / fill views on B and A ->
    placebo (200 draws x 12 sets on B) -> bootstrap -> gates -> verdict. `n_placebo` exists only so
    tests can run a cheap smoke; the registered value is st.N_PLACEBO and the CLI cannot change it."""
    say = print if verbose else (lambda *a, **k: None)
    risk_cfg = risk_cfg or ir.load_risk_cfg()
    integrity: list[str] = []
    try:
        hashes = provider.verify(family)
    except idata.SnapshotMismatch as exc:
        return dict(family=family, verdict="VOID", integrity=[f"snapshot: {exc}"])
    specs = list(fam.GRIDS[family])
    taker = ir.CostModel.flat(ir.COST_PRESETS[ir.GATING_COST])
    cut = st.oos_cut(UNIVERSES["B"]["start"], UNIVERSES["B"]["days"])
    markets: dict = {}

    override = getattr(provider, "coins", None)
    if override is not None and provider.kind != "synthetic":
        raise ValueError("a universe override is only allowed on synthetic data")

    def market(u, tf):
        if (u, tf) not in markets:
            uu = UNIVERSES[u]
            coins = override[u][tf] if override else uu["coins"][tf]
            markets[(u, tf)] = ir.Market(provider.data(u, tf), uu["start"], uu["days"], coins=coins)
        return markets[(u, tf)]

    sds: dict = {}
    grid: dict = {"B": {}, "A": {}}
    logs: list[pd.DataFrame] = []
    rej_logs: list[pd.DataFrame] = []
    for u in ("B", "A"):
        for spec in specs:
            m = market(u, spec.base_tf)
            sd = ir.SetData(m, spec)
            sds[(u, spec.set_no)] = sd
            res = ir.simulate(m, sd, taker, risk_cfg, log_rejections=True)
            if abs((res.final_equity - res.start_equity) - sum(t[ir.TRADE_FIELDS.index("net_pnl")] for t in res.trades)) > 1e-9 * res.start_equity:
                integrity.append(f"P/L reconciliation failed: {spec.set_id} {u}")
            mv = mask_violations(res, m) if spec.base_tf == "15m" else 0
            if mv:
                integrity.append(f"mask violation: {spec.set_id} {u}: {mv} trades")
            summ = run_summary(res, m, sd, cut)
            summ["mask_blocked_flat_signals"] = sum(1 for r in res.rejections if r[2] == "blocked" and r[3])
            grid[u][spec.set_id] = summ
            say(f"F{family} {u} {spec.set_id}: done ({summ['trades']} trades)")
            logs.append(_annot(ir.trades_frame(res, m, sd, u), m, cut))
            rej_logs.append(ir.rejections_frame(res, m, sd, u))
    rows = [dict(set_no=s.set_no, is_n=grid["B"][s.set_id]["is_n"], is_avgR=grid["B"][s.set_id]["is_avgR"]) for s in specs]
    ch = st.champion(rows)
    out = dict(family=family, harness_version=ir.HARNESS_VERSION, provider=provider.kind, code_commit=code_commit(),
               data_hashes=hashes, seeds=dict(placebo=st.PLACEBO_SEED, bootstrap=st.BOOT_SEED),
               constants=dict(cut=str(cut), g2_percentile=st.G2_PERCENTILE, n_boot=st.N_BOOT, n_placebo=n_placebo),
               grid=grid, selection=rows, champion=ch, integrity=integrity,
               share_positive_B=sum(1 for s in specs if (grid["B"][s.set_id]["avgR"] or 0) > 0) / len(specs))
    if ch is None:
        out["verdict"] = st.verdict(None, integrity_ok=not integrity)
        out["note"] = ("no set with >= 150 purged IS trades: gate 1 fails, family FAIL (prereg §6.2); the placebo "
                       "is still run and stored (prereg §7) but cannot change the verdict")
        shifts = st.placebo_days(st.PLACEBO_SEED, st.N_PLACEBO)[:n_placebo]
        out["placebo_shifts_days"] = shifts
        pl = run_placebo([market("B", s.base_tf) for s in specs], [sds[("B", s.set_no)] for s in specs],
                         taker, risk_cfg, cut, shifts, workers)
        out["placebo"] = dict(per_draw=pl, draw_champions=draw_champions(pl, specs))
        _write_logs(out, logs, rej_logs, logs_dir, family)
        return out
    cspec = next(s for s in specs if s.set_no == ch["set_no"])
    # ---- champion views on B and A ----
    views: dict = {"B": {}, "A": {}}
    for u in ("B", "A"):
        m = market(u, cspec.base_tf)
        sd = sds[(u, cspec.set_no)]
        for name, cm in ir.cost_views().items():
            cm = ir.tiered_cost_model(m) if cm == "tiered" else cm
            r = ir.simulate(m, sd, cm, risk_cfg)
            views[u][name] = run_summary(r, m, sd, cut)
        r_touch = ir.simulate(m, sd, taker, risk_cfg, exit_model="touch")
        views[u]["touch_exit"] = run_summary(r_touch, m, sd, cut)
        r_mk = ir.simulate(m, sd, taker, risk_cfg, fill_model="maker")
        views[u]["maker_fill"] = run_summary(r_mk, m, sd, cut)
        views[u]["maker_fill"].update(maker_detail(r_mk, m, sd, views_taker_res=ir.simulate(m, sd, taker, risk_cfg)))
    out["views"] = views
    # ---- placebo on B ----
    shifts = st.placebo_days(st.PLACEBO_SEED, st.N_PLACEBO)[:n_placebo]
    out["placebo_shifts_days"] = shifts
    mk_list = [market("B", s.base_tf) for s in specs]
    sd_list = [sds[("B", s.set_no)] for s in specs]
    say(f"F{family}: placebo {len(shifts)} draws x {len(specs)} sets, workers={workers}")
    pl = run_placebo(mk_list, sd_list, taker, risk_cfg, cut, shifts, workers)
    dch = draw_champions(pl, specs)
    out["placebo"] = dict(per_draw=pl, draw_champions=dch)
    # ---- champion statistics on B ----
    cB = grid["B"][cspec.set_id]
    cres = ir.simulate(market("B", cspec.base_tf), sds[("B", cspec.set_no)], taker, risk_cfg)
    core = cres.core()
    fin = np.isfinite(core["R"])
    R, e, x = core["R"][fin], core["entry_ns"][fin], core["exit_ns"][fin]
    mB = market("B", cspec.base_tf)
    Lb = st.block_days_for(e, x)
    boot = st.block_bootstrap(R, e, mB.start, Lb)
    boot7 = st.block_bootstrap(R, e, mB.start, st.INFO_BLOCK_DAYS)
    boot1 = st.block_bootstrap(R, e, mB.start, 1)
    fl = st.folds(e, R)
    g4 = st.g4_counts(cB["avgR"], cB["total_return"], dch)
    bench = benchmark(mB, sds[("B", cspec.set_no)])
    nb = [grid["B"][s.set_id]["avgR"] for s in fam.neighbours(cspec)]
    gi = st.GateInputs(
        n_trades=len(R), n_blocks=boot["n_blocks"], g2_lower=boot["pct"][st.G2_PERCENTILE],
        oos_n=cB["oos_n"], oos_avgR=cB["oos_avgR"], folds=fl, g4=g4, ret_taker=cB["total_return"],
        sharpe=cB["sharpe_daily"], bench_sharpe=bench["sharpe"], ret_stress=views["B"]["kraken_T3_taker_+25%"]["total_return"],
        a_avgR=grid["A"][cspec.set_id]["avgR"], a_ret=grid["A"][cspec.set_id]["total_return"],
        neighbour_avgR=[v if v is not None else float("nan") for v in nb],
    )
    gates = st.evaluate_gates(gi)
    # DSR (information only): daily-return Sharpe of the champion vs the 12 sets' spread
    srs = []
    for s in specs:
        mm = market("B", s.base_tf)
        rr = ir.simulate(mm, sds[("B", s.set_no)], taker, risk_cfg)
        de = rr.equity[:: mm.bpd]
        rets = de[1:] / de[:-1] - 1
        srs.append(float(np.mean(rets) / np.std(rets, ddof=1)) if np.std(rets) > 0 else float("nan"))
    deq = cres.equity[:: mB.bpd]
    cr = deq[1:] / deq[:-1] - 1
    from scipy.stats import kurtosis, skew

    dsr = st.deflated_sharpe(srs[specs.index(cspec)], st.N_TRIALS_G2, len(cr), float(skew(cr)),
                             float(kurtosis(cr, fisher=False)), float(np.nanvar(srs, ddof=1)))
    benchA = benchmark(market("A", cspec.base_tf), sds[("A", cspec.set_no)])
    out.update(
        champion_set=cspec.set_id,
        bootstrap=dict(block_days=Lb, gate=boot["pct"], blocks=boot["n_blocks"], DE=boot["DE"], n_eff=boot["n_eff"],
                       block7=boot7["pct"], block1=boot1["pct"], per_block_max=boot["per_block_max"],
                       per_block_median=boot["per_block_median"]),
        folds=fl, g4=g4, benchmark_B=bench, benchmark_A=benchA, btc_bh_B=benchmark(mB, None, btc_only=True),
        neighbours=[s.set_id for s in fam.neighbours(cspec)], gates={k: v for k, v in gates.items()},
        dsr_info=dsr, per_year=per_year(cres, mB, sds[("B", cspec.set_no)]),
        concentration=concentration(cres, mB), drawdown=drawdown_detail(cres, mB),
    )
    if not integrity and abs(cres.final_equity - (cB["final_equity"])) > 1e-9 * cres.start_equity:
        integrity.append("champion rerun not deterministic")
    if family == 2:
        out["f2_15m"] = f2_mask_report(market, sds, specs, taker, risk_cfg, cut, grid)
    out["verdict"] = st.verdict(gates, integrity_ok=not integrity)
    _write_logs(out, logs, rej_logs, logs_dir, family)
    return out


def _annot(df: pd.DataFrame, m: ir.Market, cut) -> pd.DataFrame:
    if not len(df):
        return df
    e = np.array([t.value for t in df["entry_time"]], dtype=np.int64)
    x = np.array([t.value for t in df["exit_time"]], dtype=np.int64)
    is_m, oos, _ = st.purged_split(e, x, cut)
    df = df.copy()
    df["period"] = np.where(is_m, "IS", np.where(oos, "OOS", "straddle"))
    df["fold"] = st.fold_of(e)
    Lb = st.block_days_for(e, x)
    df["entry_block"] = st.block_ids(e, m.start, Lb)
    return df


def _write_logs(out: dict, logs, rej_logs, logs_dir: Path | None, family: int) -> None:
    if logs_dir is None:
        return
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    p1, p2 = logs_dir / f"family{family}_trades.csv", logs_dir / f"family{family}_rejected.csv"
    (pd.concat(logs) if logs else pd.DataFrame(columns=ir.H8_COLUMNS)).to_csv(p1, index=False)
    (pd.concat(rej_logs) if rej_logs else pd.DataFrame(columns=ir.REJECTED_COLUMNS)).to_csv(p2, index=False)
    out["logs"] = {p1.name: _sha(p1), p2.name: _sha(p2)}


def mask_violations(res: ir.RunResult, m: ir.Market) -> int:
    """Trades of a masked coin whose holding span [fill open, exit] touches a masked 15m bar."""
    n = 0
    iv = idata.mask_intervals_ns()
    fi = ir.TRADE_FIELDS.index
    for t in res.trades:
        c = m.coins[t[fi("s")]]
        if not idata.is_masked_coin(c):
            continue
        a, b = t[fi("entry_ns")], t[fi("exit_ns")] + (m.step if t[fi("exit_kind")] != "end" else 0)
        if any(a < e and b > s for s, e in iv):
            n += 1
    return n


def benchmark(m: ir.Market, sd: ir.SetData | None, btc_only: bool = False) -> dict:
    daily_idx = np.arange(0, m.Tb, m.bpd)
    closes = m.Cff[:, daily_idx]
    S, D1 = closes.shape
    if btc_only:
        s = m.coins.index("BTC")
        eq = st.buy_hold_daily(closes[s:s + 1], np.array([0]))
    else:
        first_ok = np.full(S, -1)
        for s in range(S):
            ok = np.flatnonzero(sd.elig[s])
            if len(ok):
                t = sd.dec_ns[ok[0]]
                first_ok[s] = int(-(-(t - m.start.value) // ir.NS_DAY))
        masked = None
        if m.base_tf == "15m":
            masked = np.zeros((S, D1), dtype=bool)
            days = m.start.value + np.arange(D1) * ir.NS_DAY
            for s, c in enumerate(m.coins):
                if idata.is_masked_coin(c):
                    for a, b in idata.mask_intervals_ns():
                        masked[s] |= (days >= a) & (days < b)
        eq = st.buy_hold_daily(closes, first_ok, masked)
    return dict(total_return=float(eq[-1] / eq[0] - 1), max_drawdown=st.max_drawdown(eq)[0], sharpe=st.daily_sharpe(eq))


def concentration(res: ir.RunResult, m: ir.Market) -> dict:
    fi = ir.TRADE_FIELDS.index
    if not res.trades:
        return {}
    df = pd.DataFrame({"net": [t[fi("net_pnl")] for t in res.trades], "R": [t[fi("R")] for t in res.trades],
                       "day": [t[fi("entry_ns")] // ir.NS_DAY for t in res.trades],
                       "coin": [m.coins[t[fi("s")]] for t in res.trades]})
    total = df["net"].sum()
    by_day = df.groupby("day")["net"].sum().sort_values(ascending=False)
    top3 = by_day.index[:3]
    k = max(1, int(math.ceil(0.01 * len(df))))
    rest = df[~df["day"].isin(top3)]
    per_coin = {c: dict(n=int(len(g)), avgR=float(g["R"].mean())) for c, g in df.groupby("coin")}
    return dict(top3_days_share=float(by_day.iloc[:3].sum() / total) if total else float("nan"),
                top1pct_trades_share=float(df["net"].nlargest(k).sum() / total) if total else float("nan"),
                avgR_ex_top3_days=float(rest["R"].mean()) if len(rest) else float("nan"),
                return_ex_top3_days=float(rest["net"].sum() / res.start_equity), per_coin=per_coin,
                coins_positive=int(sum(1 for v in per_coin.values() if v["avgR"] > 0)))


def drawdown_detail(res: ir.RunResult, m: ir.Market) -> dict:
    dd, pk, tr = st.max_drawdown(res.equity)
    eq = res.equity
    rec = np.flatnonzero(eq[tr:] >= eq[pk])
    end_i = tr + int(rec[0]) if len(rec) else len(eq) - 1
    deq = eq[:: m.bpd]
    r1 = deq[1:] / deq[:-1] - 1
    r7 = deq[7:] / deq[:-7] - 1 if len(deq) > 7 else np.array([np.nan])
    ts = lambda k: str(pd.Timestamp(int(m.base_ns[k]), tz="UTC"))  # noqa: E731
    return dict(max_drawdown=dd, peak=ts(pk), trough=ts(tr), recovered=ts(end_i) if len(rec) else None,
                duration_days=float((m.base_ns[end_i] - m.base_ns[pk]) / ir.NS_DAY),
                worst_day=float(np.min(r1)) if len(r1) else float("nan"), worst_7d=float(np.nanmin(r7)))


def maker_detail(r_mk: ir.RunResult, m: ir.Market, sd: ir.SetData, views_taker_res: ir.RunResult) -> dict:
    fi = ir.TRADE_FIELDS.index
    filled = len(r_mk.trades)
    missed = len(r_mk.missed)
    taker_by_sig = {(t[fi("s")], t[fi("d_signal")]): t[fi("R")] for t in views_taker_res.trades}
    missed_R = [taker_by_sig[(s, d)] for s, d, _ in r_mk.missed if (s, d) in taker_by_sig]
    post = {1: [], 4: []}
    for t in r_mk.trades:
        s, k, px = t[fi("s")], t[fi("fill_k")], t[fi("entry_px")]
        for h in (1, 4):
            kk = k + h * int(3600 // (m.step / 1e9))
            if kk < m.Tb and np.isfinite(m.Cff[s, kk]):
                post[h].append(m.Cff[s, kk] / px - 1)
    return dict(orders=filled + missed, filled=filled, fill_rate=filled / (filled + missed) if filled + missed else float("nan"),
                missed=missed, missed_taker_avgR=float(np.mean(missed_R)) if missed_R else float("nan"),
                post_fill_ret_1h=float(np.mean(post[1])) if post[1] else float("nan"),
                post_fill_ret_4h=float(np.mean(post[4])) if post[4] else float("nan"))


def per_year(res: ir.RunResult, m: ir.Market, sd: ir.SetData) -> list[dict]:
    fi = ir.TRADE_FIELDS.index
    e = np.array([t[fi("entry_ns")] for t in res.trades], dtype=np.int64)
    R = np.array([t[fi("R")] for t in res.trades], dtype=float)
    out = []
    spans = [(f[0], f[1], f[2]) for f in st.FOLDS]
    for y in range(m.start.year, m.end.year + 1):
        a = max(pd.Timestamp(f"{y}-01-01", tz="UTC"), m.start)
        b = min(pd.Timestamp(f"{y + 1}-01-01", tz="UTC"), m.end)
        spans.append((str(y), a, b))
    for name, a, b in spans:
        msk = (e >= a.value) & (e < b.value)
        ka = int((max(a, m.start).value - m.start.value) // m.step)
        kb = int((min(b, m.end).value - m.start.value) // m.step)
        seg = res.equity[ka:kb + 1]
        da, db = int((a.value - sd.dec_ns[0]) // (sd.dec_ns[1] - sd.dec_ns[0])), int((b.value - sd.dec_ns[0]) // (sd.dec_ns[1] - sd.dec_ns[0]))
        reg = sd.regime[max(0, da):max(0, db)]
        out.append(dict(period=name, trades=int(msk.sum()), avgR=float(R[msk].mean()) if msk.any() else float("nan"),
                        ret=float(seg[-1] / seg[0] - 1) if len(seg) > 1 else float("nan"),
                        maxDD=st.max_drawdown(seg)[0] if len(seg) > 1 else float("nan"),
                        regime_on_share=float(reg.mean()) if len(reg) else float("nan")))
    return out


def f2_mask_report(market, sds, specs, taker, risk_cfg, cut, grid) -> dict:
    out = {}
    for s in specs:
        if s.tf != "15m":
            continue
        m = market("B", "15m")
        sd = sds[("B", s.set_no)]
        usable = {}
        is_days = int((cut - m.start) / pd.Timedelta(days=1))
        day_of = (sd.dec_ns - m.start.value) // ir.NS_DAY
        for i, c in enumerate(m.coins):
            okd = np.unique(day_of[(sd.mask_ok[i] & sd.has_dec[i]) & (sd.dec_ns < cut.value)])
            usable[c] = int(len(okd))
        core_coins = tuple(c for c in CORE_15M if c in m.coins)
        mc = ir.Market(m.data, m.start, m.days, coins=core_coins)
        rc = ir.simulate(mc, ir.SetData(mc, s), taker, risk_cfg)
        out[s.set_id] = dict(usable_is_days=usable, is_days=is_days,
                             mask_blocked_flat_signals=grid["B"][s.set_id].get("mask_blocked_flat_signals", 0),
                             core_only=light_metrics(rc, cut))
    return out


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def _p(x, pct=False, nd=2):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    return f"{x * 100:.1f}%" if pct else f"{x:.{nd}f}"


def _table(header, rows):
    return "\n".join(["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] +
                     ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def render_report(r: dict) -> str:
    f = r["family"]
    lines = [f"# Intraday P2, family {f}: {r.get('verdict')}", ""]
    surv = ("**Verdict: {v}.** Survivorship: the coins are today's survivors, chosen with hindsight (prereg §1.2). "
            "Prints are Coinbase, not Kraken; Coinbase volume is 1-3x Kraken's and supports no capacity claim. "
            "{mask}No rule-0.2 rerun. Harness `{h}`, code `{c}`, data provider `{p}`.").format(
        v=r.get("verdict"), h=r.get("harness_version"), c=r.get("code_commit"), p=r.get("provider"),
        mask=("15m sets: every coin except BTC/ETH is masked on 2023-03-04, 2024-08-13..09-03 and "
              "2025-01-01..04-30; masked windows are excluded from the 15m benchmark. " if f == 2 else ""))
    lines += [surv, ""]
    if r.get("integrity"):
        lines += ["**Integrity failures (VOID):** " + "; ".join(r["integrity"]), ""]
    ch = r.get("champion")
    lines += ["## 1. Grid table (kraken_T3_taker)", ""]
    hdr = ["set", "U", "trades", "blocks", "win%", "avg R", "avg R gross", "drag R", "CI 2.5%", "CI 0.0694%", "PF",
           "return", "maxDD", "Sharpe", "expo", "turnover/y", "hold med/mean d", "IS n / avg R", "OOS n / avg R"]
    rows = []
    for u in ("B", "A"):
        for sid, g in r["grid"][u].items():
            b = g.get("boot", {}).get("pct", {})
            mark = " **(champion)**" if ch and sid == r.get("champion_set") and u == "B" else ""
            rows.append([sid + mark, u, g["trades"], g.get("n_blocks"), _p(g.get("win_rate"), True), _p(g.get("avgR")),
                         _p(g.get("avgR_gross")), _p(g.get("cost_drag_R")), _p(b.get("2.5")), _p(b.get(str(st.G2_PERCENTILE))),
                         _p(g.get("profit_factor")), _p(g["total_return"], True), _p(g["max_drawdown"], True),
                         _p(g["sharpe_daily"]), _p(g["exposure"], True), _p(g.get("turnover_annual")),
                         f"{_p(g.get('hold_days_median'))} / {_p(g.get('hold_days_mean'))}",
                         f"{g.get('is_n')} / {_p(g.get('is_avgR'))}", f"{g.get('oos_n')} / {_p(g.get('oos_avgR'))}"])
    lines += [_table(hdr, rows), ""]
    lines += ["Selection: highest purged IS avg R among sets with >= 150 purged IS trades (ties: more IS trades, lower "
              f"set number). Champion: **{r.get('champion_set', 'none')}**. Universe A IS/OOS columns use B's cut "
              "(information only). Share of the 12 sets with positive B avg R: " + _p(r.get("share_positive_B"), True), ""]
    if not ch:
        lines += [r.get("note", ""), ""]
        return "\n".join(lines) + "\n"
    g = r["gates"]
    bs = r["bootstrap"]
    lines += ["## 2. Gate table (champion, universe B, kraken_T3_taker)", ""]
    cB = r["grid"]["B"][r["champion_set"]]
    gate_rows = [
        ["G1", f"{cB['trades']} trades, {bs['blocks']} entry blocks (need 300 / 100)", g["G1"]],
        ["G2", f"0.0694th pct of block bootstrap avg R = {_p(bs['gate'][st.G2_PERCENTILE], nd=4)} (L_b = {bs['block_days']} d); "
               f"info: 0.139th {_p(bs['gate'][0.139], nd=4)}, 2.5th {_p(bs['gate'][2.5], nd=4)}", g["G2"]],
        ["G3", f"OOS n {g['G3_detail']['oos_n']} avg R {_p(g['G3_detail']['oos_avgR'])}; folds scored {g['G3_detail']['scored']}, losing {g['G3_detail']['losing']}", g["G3"]],
        ["G4", f"draw-champions >= real: avg R {r['g4']['ge_avgR']}, return {r['g4']['ge_return']} (max 2); draws without a champion {r['g4']['no_champion']} (counted as not beaten)", g["G4"]],
        ["G5", f"return {_p(cB['total_return'], True)}, Sharpe {_p(cB['sharpe_daily'])} vs bench {_p(r['benchmark_B']['sharpe'])}; +25% return {_p(r['views']['B']['kraken_T3_taker_+25%']['total_return'], True)}", g["G5"]],
        ["R1", f"A avg R {_p(r['grid']['A'][r['champion_set']]['avgR'])}, return {_p(r['grid']['A'][r['champion_set']]['total_return'], True)}", g["R1"]],
        ["R2", f"neighbours {r['neighbours']}: {g['R2_detail']['positive']} of {g['R2_detail']['neighbours']} positive (need {g['R2_detail']['needed']})", g["R2"]],
    ]
    lines += [_table(["gate", "numbers", "pass"], [[a, b, "PASS" if c else "FAIL"] for a, b, c in gate_rows]), ""]
    lines += [_table(["benchmark", "return", "maxDD", "Sharpe"], [
        ["EW buy&hold B", _p(r["benchmark_B"]["total_return"], True), _p(r["benchmark_B"]["max_drawdown"], True), _p(r["benchmark_B"]["sharpe"])],
        ["EW buy&hold A", _p(r["benchmark_A"]["total_return"], True), _p(r["benchmark_A"]["max_drawdown"], True), _p(r["benchmark_A"]["sharpe"])],
        ["BTC buy&hold B", _p(r["btc_bh_B"]["total_return"], True), _p(r["btc_bh_B"]["max_drawdown"], True), _p(r["btc_bh_B"]["sharpe"])]]), "",
        f"Deflated Sharpe (N = 36, information only): {_p(r.get('dsr_info'), nd=3)}", ""]
    lines += ["## 3. Per-year table", "", _table(["period", "trades", "avg R", "return", "maxDD", "regime-ON share"],
              [[y["period"], y["trades"], _p(y["avgR"]), _p(y["ret"], True), _p(y["maxDD"], True), _p(y["regime_on_share"], True)] for y in r["per_year"]]), ""]
    lines += ["## 4. Cost, exit-model and fill-model sensitivity (champion)", ""]
    vr = []
    for u in ("B", "A"):
        for name, v in r["views"][u].items():
            b = v.get("boot", {}).get("pct", {})
            vr.append([u, name, v["trades"], _p(v.get("avgR")), f"[{_p(b.get('2.5'))}, {_p(b.get('97.5'))}]", _p(v["total_return"], True), _p(v["sharpe_daily"])])
    lines += [_table(["U", "view", "trades", "avg R", "CI 2.5-97.5", "return", "Sharpe"], vr), ""]
    mk = r["views"]["B"]["maker_fill"]
    lines += [f"Maker model (B): orders {mk.get('orders')}, fill rate {_p(mk.get('fill_rate'), True)}, missed {mk.get('missed')}, "
              f"missed trades' taker avg R {_p(mk.get('missed_taker_avgR'))}, mean post-fill return 1h {_p(mk.get('post_fill_ret_1h'), True)} / "
              f"4h {_p(mk.get('post_fill_ret_4h'), True)}. A maker result can never turn a FAIL into a PASS.", ""]
    lines += ["## 5. Cost geometry", "", _table(["U", "median stop % price", "cost drag R", "gross % / trade", "gross % / day held"],
              [[u, _p(r["grid"][u][r["champion_set"]].get("median_stop_pct"), True), _p(r["grid"][u][r["champion_set"]].get("cost_drag_R")),
                _p(r["grid"][u][r["champion_set"]].get("gross_pct_per_trade"), True), _p(r["grid"][u][r["champion_set"]].get("gross_pct_per_day"), True)] for u in ("B", "A")]), ""]
    lines += ["## 6. Turnover", "", f"Traded notional / average equity, annualised: {_p(cB.get('turnover_annual'))}; trades per week "
              f"{_p(cB.get('trades_per_week'))}; fees paid {_p(cB.get('fees_pct_start'), True)} of starting equity.", ""]
    dd = r["drawdown"]
    lines += ["## 7. Drawdown", "", f"Max drawdown {_p(dd['max_drawdown'], True)} from {dd['peak']} to {dd['trough']}, recovered "
              f"{dd['recovered']} ({_p(dd['duration_days'], nd=0)} days); worst day {_p(dd['worst_day'], True)}; worst 7 days {_p(dd['worst_7d'], True)}.", ""]
    cc = r["concentration"]
    lines += ["## 8. Concentration", "", f"Share of net P/L from the top 3 entry days {_p(cc.get('top3_days_share'), True)}, from the top 1% of "
              f"trades {_p(cc.get('top1pct_trades_share'), True)}; without the top 3 entry days avg R {_p(cc.get('avgR_ex_top3_days'))}, "
              f"return {_p(cc.get('return_ex_top3_days'), True)}; coins with positive avg R {cc.get('coins_positive')}.", "",
              _table(["coin", "trades", "avg R"], [[c, v["n"], _p(v["avgR"])] for c, v in sorted(cc.get("per_coin", {}).items())]), ""]
    lines += ["## 9. Correlation", "", f"Design effect {_p(bs['DE'])}, n_eff {_p(bs['n_eff'], nd=1)}; 1-day-block CI lower 2.5% "
              f"{_p(bs['block1'][2.5], nd=4)} vs 7-day {_p(bs['block7'][2.5], nd=4)}; positions opened per block max {bs['per_block_max']}, median {_p(bs['per_block_median'], nd=1)}.", ""]
    lines += ["## 10. Risk-engine mix", "", f"Checks {cB['checks']}, approvals {cB['approvals']}, rejections by reason {cB['reject_reasons']}; dust (< $50) share {_p(cB.get('dust_share'), True)}.", ""]
    dch = r["placebo"]["draw_champions"]
    vals_R = [x["avgR"] for x in dch if x]
    vals_ret = [x["total_return"] for x in dch if x]
    sel = pd.Series([x["set_no"] for x in dch if x]).value_counts().sort_index().to_dict() if vals_R else {}
    pct = lambda v: [_p(float(np.percentile(v, q))) for q in (5, 50, 95, 99)] if v else ["n/a"] * 4  # noqa: E731
    lines += ["## 11. Placebo", "", f"{len(dch)} draws (shifts {r['placebo_shifts_days'][:5]}..., default_rng(0)); draw-champion avg R "
              f"p5/p50/p95/p99 {pct(vals_R)}; return p5/p50/p95/p99 {pct(vals_ret)}; selected sets {sel}; draws without a "
              f"champion {r['g4']['no_champion']}. Real champion: {r['g4']['ge_avgR']} draws >= on avg R, {r['g4']['ge_return']} on return.", ""]
    if f == 2:
        lines += ["## 12. 15m-specific", "", "The IS/OOS cut (2025-04-14) falls inside the 2025-01-01..04-30 mask, so the in-sample window of "
                  "the 15m sets is shortened for every masked coin (no entries from about 2024-12-31 to the cut, plus the earlier masks).", ""]
        for sid, v in r.get("f2_15m", {}).items():
            lines += [f"- {sid}: mask-blocked flat signals {v['mask_blocked_flat_signals']}; usable IS days per coin {v['usable_is_days']} of {v['is_days']}; "
                      f"core-only (BTC, ETH, SOL, LTC) trades {v['core_only']['n']}, avg R {_p(v['core_only']['avgR'])}, return {_p(v['core_only']['total_return'], True)}"]
        lines += [""]
    lines += ["## 13. Logs", "", f"Per-trade and rejected-signal logs (gitignored), SHA-256: {r.get('logs', {})}", ""]
    return "\n".join(lines) + "\n"


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return _f(float(o))
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, pd.Timestamp):
        return str(o)
    return o


def dumps(results) -> str:
    return json.dumps(_jsonable(results), sort_keys=True, indent=1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--family", choices=["1", "2", "3", "all"], required=True)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--data-root", default=None, help=f"default ${idata.DATA_ROOT_ENV} or {idata.DATA_ROOT_DEFAULT}")
    ap.add_argument("--out-json", default=str(DEFAULT_JSON))
    ap.add_argument("--docs-dir", default=str(DEFAULT_DOCS))
    ap.add_argument("--logs-dir", default=str(DEFAULT_LOGS))
    a = ap.parse_args(argv)
    print(f"Harness version: {ir.HARNESS_VERSION}")
    print(f"Code commit: {code_commit()}")
    prov = RealProvider(a.data_root)
    fams = [1, 2, 3] if a.family == "all" else [int(a.family)]
    results = {}
    for f in fams:
        try:
            print("Data hashes:", prov.verify(f))
        except idata.SnapshotMismatch as exc:
            print(f"VOID: {exc}")
            results[f"family{f}"] = dict(family=f, verdict="VOID", integrity=[str(exc)])
            continue
        r = evaluate_family(f, prov, a.workers, Path(a.logs_dir))
        results[f"family{f}"] = r
        Path(a.docs_dir, f"intraday_p2_family{f}.md").write_text(render_report(r))
        print(f"family {f}: {r['verdict']}")
    Path(a.out_json).write_text(dumps(dict(harness_version=ir.HARNESS_VERSION, results=results)))


if __name__ == "__main__":
    main()
