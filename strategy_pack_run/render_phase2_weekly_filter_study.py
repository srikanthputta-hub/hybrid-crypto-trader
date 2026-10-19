"""render_phase2_weekly_filter_study.py -- renders results.json from run_phase2_weekly_filter_study.py into the
markdown tables used in docs/phase2_weekly_filter_study.md §1+ and applies the pre-registered decision rule
(doc §0.7) mechanically. Pure formatting + the rule; reads only the JSON.

Usage (from strategy_pack_run/):
    python render_phase2_weekly_filter_study.py --out-dir phase2_weekly_filter_study
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
CANDS = ("W1", "W2", "W3")
GATE_NAMES = ("trades>=100", "CI low>0", "later30>0", "placebo>=90", "ret>0&Sharpe>BH")


def f(x, nd=2, pct=False):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x * 100:.1f}%" if pct else f"{x:.{nd}f}"


def gates_str(g):
    return " ".join("P" if x else "F" for x in g) + f" ({sum(g)}/5)"


def decide(jobs_by_key, cand):
    """Pre-registered rule §0.7. Returns (promising, reasons-failed list)."""
    fails = []
    for d in ("data", "data_long"):
        for k in (0, 7, 14, 21):
            j = jobs_by_key[(d, k)]
            c = j["comparisons"][cand]
            if c["gate_regressions"]:
                fails.append(f"(i) {d} k={k}: regresses {', '.join(c['gate_regressions'])}")
            if not c["n_ratio"] >= 0.7:
                fails.append(f"(iv) {d} k={k}: trade ratio {c['n_ratio']:.2f} < 0.70")
            if not c["delta"] > 0:
                fails.append(f"(v) {d} k={k}: delta {c['delta']:+.3f} <= 0")
        c0 = jobs_by_key[(d, 0)]["comparisons"][cand]
        if not c0["paired"]["lo"] > 0:
            fails.append(f"(ii) {d}: paired day-block CI low {c0['paired']['lo']:+.3f} <= 0")
        if not c0["later_delta"] > 0:
            fails.append(f"(iii-a) {d}: later-30% delta {c0['later_delta']:+.3f} <= 0")
        bad = {y: v for y, v in c0["loyo"].items() if not v > 0}
        if bad:
            fails.append(f"(iii-b) {d}: leave-one-year-out delta <= 0 without " + ", ".join(f"{y} ({v:+.3f})" for y, v in bad.items()))
    return (not fails), fails


def render(payload) -> str:
    jobs = {(j["dataset"], j["offset"]): j for j in payload["jobs"]}
    L = []
    # 1. headline at offset 0
    L.append("### Offset 0 (canonical): all runs\n")
    L.append("| dataset | run | signals | veto | trades | win% | avg R [iid 95% CI] | day-block CI | return | maxDD | Sharpe (EW BH) | later-30% n / avg R | placebo pct ret / R | gates 1-5 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for d in ("data", "data_long"):
        j = jobs[(d, 0)]
        for name, r in j["results"].items():
            L.append(
                f"| `{d}/` | {name} | {r['signals']} | {f(r['veto_rate'], pct=True)} | {r['trades']} | {f(r['win_rate'], pct=True)} | "
                f"{f(r['avg_R'])} [{f(r['ci_iid'][0])}, {f(r['ci_iid'][1])}] | [{f(r['ci_day'][0])}, {f(r['ci_day'][1])}] | "
                f"{f(r['total_return'], pct=True)} | {f(r['max_dd'], pct=True)} | {f(r['sharpe'])} ({f(r['bh_sharpe'])}) | "
                f"{r['later_n']} / {f(r['later_R'])} | {r['placebo_pct_ret'] * 100:.0f} / {r['placebo_pct_R'] * 100:.0f} | {gates_str(r['gates'])} |"
            )
    # 2. comparisons at offset 0
    L.append("\n### Offset 0: candidate vs baseline (decision inputs)\n")
    L.append("| dataset | cand | Δ avg R | paired day-block 95% CI of Δ | later-30% Δ | leave-one-year-out Δ | n ratio | removed n / avg R | added n / avg R | common | gate regressions |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for d in ("data", "data_long"):
        j = jobs[(d, 0)]
        for name, c in j["comparisons"].items():
            loyo = ", ".join(f"{y}: {v:+.2f}" for y, v in c["loyo"].items())
            dd = c["diff"]
            L.append(
                f"| `{d}/` | {name} | {c['delta']:+.3f} | [{c['paired']['lo']:+.3f}, {c['paired']['hi']:+.3f}] | {c['later_delta']:+.3f} | {loyo} | "
                f"{c['n_ratio']:.2f} | {dd['removed_n']} / {f(dd['removed_avg_R'])} | {dd['added_n']} / {f(dd['added_avg_R'])} | {dd['common']} | "
                f"{', '.join(c['gate_regressions']) or 'none'} |"
            )
    # 3. per-year
    L.append("\n### Per entry year, offset 0 (n / avg R)\n")
    for d in ("data", "data_long"):
        j = jobs[(d, 0)]
        names = ["B", *CANDS]
        years = sorted({y for n in names for y in j["results"][n]["per_year"]})
        L.append(f"`{d}/`:\n")
        L.append("| year | " + " | ".join(names) + " |")
        L.append("|---|" + "---|" * len(names))
        for y in years:
            cells = []
            for n in names:
                v = j["results"][n]["per_year"].get(y)
                cells.append(f"{v[0]} / {v[1]:+.2f}" if v else "0 / n/a")
            L.append(f"| {y} | " + " | ".join(cells) + " |")
        L.append("")
    # 4. offsets
    L.append("### Start-offset sensitivity (every symbol drops its first k bars)\n")
    L.append("| dataset | k | run | trades | avg R | day-block CI | return | Sharpe | later-30% R | placebo ret / R | gates | Δ vs B | paired CI of Δ |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for d in ("data", "data_long"):
        for k in (0, 7, 14, 21):
            j = jobs[(d, k)]
            for name in ["B", *CANDS]:
                r = j["results"][name]
                c = j["comparisons"].get(name)
                delta = "" if c is None else "{:+.3f}".format(c["delta"])
                pci = "" if c is None else "[{:+.3f}, {:+.3f}]".format(c["paired"]["lo"], c["paired"]["hi"])
                L.append(
                    f"| `{d}/` | {k} | {name} | {r['trades']} | {f(r['avg_R'], 3)} | [{f(r['ci_day'][0])}, {f(r['ci_day'][1])}] | "
                    f"{f(r['total_return'], pct=True)} | {f(r['sharpe'])} | {f(r['later_R'])} | {r['placebo_pct_ret'] * 100:.0f} / {r['placebo_pct_R'] * 100:.0f} | "
                    f"{gates_str(r['gates'])} | {delta} | {pci} |"
                )
    # 5. gate coverage
    L.append("\n### Share of symbol-days each gate is ON (own listed days)\n")
    for d, cov in payload["gate_on_share"].items():
        L.append(f"- `{d}/`: " + ", ".join(f"{k} {v * 100:.1f}%" for k, v in cov.items()))
    # 6. verdict
    L.append("\n### Mechanical verdict (rule §0.7)\n")
    for cand in CANDS:
        ok, fails = decide(jobs, cand)
        L.append(f"- **{cand}: {'PROMISING' if ok else 'NOT PROMISING'}**" + ("" if ok else f" ({len(fails)} failed conditions)"))
        for x in fails:
            L.append(f"  - {x}")
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="phase2_weekly_filter_study")
    a = ap.parse_args()
    d = _THIS_DIR / a.out_dir
    text = render(json.loads((d / "results.json").read_text(encoding="utf-8")))
    (d / "summary.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
