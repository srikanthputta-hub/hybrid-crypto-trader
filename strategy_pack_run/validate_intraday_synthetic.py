"""validate_intraday_synthetic.py - validates run_intraday_p2.evaluate_family on SYNTHETIC data only
(docs/intraday_prereg.md §0 rule 3: no strategy may be evaluated on the real evaluation data before
the one-shot P2 run). Everything runs the full registered sequence on the frozen windows (B and A,
cut 2025-04-14, 200 placebo draws, 50,000 bootstrap resamples); only the data are synthetic and the
universe is smaller (the synthetic-only coins override).

  planted : family 1 on persistent-trend data (drift switching +mu / -mu in multi-day segments,
            zero mean): a real timing edge that a shifted copy of the signals does not have.
  noise   : family 1 on pure random walks (every coin, BTC included): the false-pass rate per gate
            and overall, over --n datasets (seeds 1000..).

    python strategy_pack_run/validate_intraday_synthetic.py planted --workers 4 --out planted.json
    python strategy_pack_run/validate_intraday_synthetic.py noise --n 100 --workers 4 --out noise.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import intraday_synthetic as syn  # noqa: E402
import run_intraday_p2 as p2  # noqa: E402

N_HOURS = 24 * 2240          # 2020-09-01 .. 2026-10-20: warm-up for SMA(200) / Wilder + both windows
DATA_START = "2020-09-01"
GATES = ("G1", "G2", "G3", "G4", "G5", "R1", "R2")


def provider(coins, make):
    cache = {}

    def factory(u, tf):
        if tf not in cache:
            cache[tf] = make(tf)
        return cache[tf]

    return p2.SyntheticProvider(factory, coins={u: {"1h": coins, "15m": coins} for u in ("B", "A")})


def summarize(r: dict) -> dict:
    out = dict(verdict=r["verdict"], champion=r.get("champion_set"))
    if r.get("gates"):
        out["gates"] = {k: bool(r["gates"][k]) for k in GATES}
        ch = r["champion_set"]
        cB = r["grid"]["B"][ch]
        out.update(trades=cB["trades"], avgR=cB["avgR"], ret=cB["total_return"], sharpe=cB["sharpe_daily"],
                   bench_sharpe=r["benchmark_B"]["sharpe"], g2_lower=r["bootstrap"]["gate"][0.0694],
                   ci_2_5=r["bootstrap"]["gate"][2.5], g4=r["g4"], oos=(cB["oos_n"], cB["oos_avgR"]),
                   folds=[(f["fold"], f["n"], round(f["avgR"], 3) if f["n"] else None) for f in r["folds"]],
                   stress_ret=r["views"]["B"]["kraken_T3_taker_+25%"]["total_return"],
                   A=(r["grid"]["A"][ch]["avgR"], r["grid"]["A"][ch]["total_return"]),
                   placebo_champion_avgR_p50=sorted(x["avgR"] for x in r["placebo"]["draw_champions"] if x)[100]
                   if sum(1 for x in r["placebo"]["draw_champions"] if x) > 100 else None)
    return out


def planted(workers: int, seed: int = 3, mu: float = 0.0008, n_coins: int = 8) -> dict:
    names = ["ETH", "SOL", "ADA", "DOGE", "AVAX", "LINK", "LTC", "DOT"][:n_coins - 1]
    coins = tuple(sorted(("BTC", *names)))
    prov = provider(coins, lambda tf: syn.universe_1h(names, DATA_START, N_HOURS, seed=seed, kind="regime_switch",
                                                      sigma=0.006, mu=mu))
    t0 = time.time()
    r = p2.evaluate_family(1, prov, workers=workers, verbose=False)
    s = summarize(r)
    s["seconds"] = round(time.time() - t0, 1)
    s["setup"] = dict(family=1, coins=coins, mu_per_hour=mu, sigma_per_hour=0.006, seed=seed)
    return s


def noise(n: int, workers: int, first_seed: int = 1000, n_coins: int = 4) -> dict:
    names = ["ETH", "SOL", "ADA"][:n_coins - 1]
    coins = tuple(sorted(("BTC", *names)))
    runs = []
    t0 = time.time()
    for i in range(n):
        seed = first_seed + i
        prov = provider(coins, lambda tf, seed=seed: syn.universe_1h(names, DATA_START, N_HOURS, seed=seed, kind="random_walk",
                                                                     sigma=0.006, btc_drift=0.0, btc_sigma=0.004))
        r = p2.evaluate_family(1, prov, workers=workers, verbose=False)
        s = summarize(r)
        s["seed"] = seed
        runs.append(s)
        print(f"[{i + 1}/{n}] seed {seed}: {s['verdict']} {s.get('gates')} ({time.time() - t0:.0f}s)", flush=True)
    with_ch = [x for x in runs if "gates" in x]
    rates = {g: sum(x["gates"][g] for x in with_ch) for g in GATES}
    verdicts = {v: sum(1 for x in runs if x["verdict"] == v) for v in ("PASS", "INCONCLUSIVE", "FAIL", "VOID")}
    return dict(n=n, with_champion=len(with_ch), gate_pass_counts=rates, verdicts=verdicts,
                seconds=round(time.time() - t0, 1), setup=dict(family=1, coins=coins, seeds=[first_seed, first_seed + n - 1]),
                runs=runs)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["planted", "noise"])
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    res = planted(a.workers) if a.mode == "planted" else noise(a.n, a.workers)
    Path(a.out).write_text(p2.dumps(res))
    print(json.dumps({k: v for k, v in res.items() if k != "runs"}, indent=1, default=str))


if __name__ == "__main__":
    main()
