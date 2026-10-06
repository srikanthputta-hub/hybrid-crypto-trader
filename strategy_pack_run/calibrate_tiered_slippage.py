"""calibrate_tiered_slippage.py - one-off, manual calibration check for tiered_slippage.py's
pre-registered a-priori tiers against REAL Kraken order-book spreads.

P1d of docs/system-redesign-2026-10.md (§4.4 rule 3: "Calibrate once against real spreads (one
fetch_order_book per universe symbol, about 150 calls) before any gate run, then freeze the
tiers."). This script REPORTS the comparison -- it never adjusts TIERS_USD_VOLUME_TO_SLIPPAGE
itself. If the comparison suggests a tier is wrong, that's a finding for the report, not something
this script silently fixes (design doc: "don't silently adjust the tiers based on it").

Not a pytest-collected test (no network access in CI/test runs) -- this is a manual, one-off
script, run directly:

  python strategy_pack_run/calibrate_tiered_slippage.py \\
      --kraken-dir data_universe/raw_kraken --quote USD \\
      --out data_universe/tiered_slippage_calibration.md

What it does, per universe symbol:
  1. Reads that symbol's OWN already-fetched kraken_720_universe CSV (no re-fetch of OHLCV) to
     compute its most-recent trailing-30-day median dollar volume (same trailing_quote_volume
     formula backtest_live_bot.py and rank_signals both use) -- this decides which a priori tier
     tiered_slippage.py would assign it.
  2. Fetches ONE live order book from Kraken (fetch_order_book) and computes the realized
     bid-ask spread, both FULL spread and HALF spread (ask-mid)/mid -- half-spread is the more
     directly comparable number to a single-side `Costs.slippage` fraction (a slippage fraction is
     paid once per fill, roughly like crossing half the spread), full spread is reported too since
     it's the more commonly quoted number.
  3. Compares the assigned tier's assumed slippage (bps) against the realized half-spread (bps)
     and reports the gap, grouped by tier -- does NOT change the tier.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import ccxt
import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from backtest_live_bot import load_csv, trailing_quote_volume  # noqa: E402
from tiered_slippage import TIERS_USD_VOLUME_TO_SLIPPAGE, tiered_slippage_fraction  # noqa: E402

MAX_FETCH_RETRIES = 5
RETRY_BASE_SLEEP_S = 2.0


def _fetch_order_book_with_retry(ex: ccxt.Exchange, symbol: str) -> dict | None:
    for attempt in range(MAX_FETCH_RETRIES):
        try:
            return ex.fetch_order_book(symbol, limit=10)
        except (ccxt.RateLimitExceeded, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as exc:
            if attempt == MAX_FETCH_RETRIES - 1:
                print(f"  ... giving up on {symbol} after {MAX_FETCH_RETRIES} attempts ({exc})", flush=True)
                return None
            sleep_s = RETRY_BASE_SLEEP_S * (2 ** attempt)
            time.sleep(sleep_s)
        except ccxt.BadSymbol:
            return None
    return None


def median_quote_volume_from_csv(path: Path, window: int = 30) -> float | None:
    """Most recent trailing-30-bar median dollar volume for one symbol's already-fetched daily
    CSV -- the same trailing_quote_volume formula used everywhere else in this harness, just
    evaluated at the LAST available bar (there's no point-in-time concern here: this is a
    present-day classification for a present-day order-book snapshot, not a backtest decision)."""
    df = load_csv(path, "1d")
    if df.empty:
        return None
    tqv = trailing_quote_volume(df, window=window)
    val = tqv.iloc[-1]
    return None if pd.isna(val) else float(val)


def tier_label(floor: float) -> str:
    if floor >= 10_000_000.0:
        return ">=$10M (5bp)"
    if floor >= 1_000_000.0:
        return "$1M-$10M (20bp)"
    return "<$1M (50bp)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kraken-dir", default="data_universe/raw_kraken",
                     help="Directory of fetch_kraken.py --universe discover's per-symbol CSVs.")
    ap.add_argument("--quote", default="USD")
    ap.add_argument("--out", default="data_universe/tiered_slippage_calibration.md")
    ap.add_argument("--limit", type=int, default=None,
                     help="Optional cap on how many symbols to check (for a quick smoke run).")
    a = ap.parse_args()

    kraken_dir = Path(a.kraken_dir)
    csv_paths = sorted(kraken_dir.glob(f"*_{a.quote}_1d.csv"))
    if a.limit:
        csv_paths = csv_paths[: a.limit]
    print(f"{len(csv_paths)} symbol CSV(s) found under {kraken_dir}", flush=True)

    ex = ccxt.kraken({"enableRateLimit": True})
    ex.session.trust_env = True  # see fetch_kraken.py for why this is needed in a proxied env
    ex.load_markets()

    rows = []
    for n, path in enumerate(csv_paths, start=1):
        base = path.stem.split("_")[0]
        symbol = f"{base}/{a.quote}"
        if symbol not in ex.markets:
            continue
        median_vol = median_quote_volume_from_csv(path)
        ob = _fetch_order_book_with_retry(ex, symbol)
        if ob is None or not ob.get("bids") or not ob.get("asks"):
            print(f"[{n}/{len(csv_paths)}] {symbol}: no order book data, skipping", flush=True)
            continue
        bid, ask = ob["bids"][0][0], ob["asks"][0][0]
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            continue
        mid = (bid + ask) / 2.0
        full_spread_bps = (ask - bid) / mid * 10_000.0
        half_spread_bps = full_spread_bps / 2.0
        assumed_fraction = tiered_slippage_fraction(median_vol)
        assumed_bps = assumed_fraction * 10_000.0
        rows.append(dict(
            symbol=symbol, median_quote_volume_usd=median_vol,
            tier=tier_label(10_000_000.0 if median_vol and median_vol >= 10_000_000.0
                            else (1_000_000.0 if median_vol and median_vol >= 1_000_000.0 else 0.0)),
            assumed_bps=assumed_bps, half_spread_bps=half_spread_bps, full_spread_bps=full_spread_bps,
            gap_bps=half_spread_bps - assumed_bps,
        ))
        print(f"[{n}/{len(csv_paths)}] {symbol}: median_vol=${median_vol:,.0f} assumed={assumed_bps:.1f}bp "
              f"half_spread={half_spread_bps:.2f}bp" if median_vol is not None else
              f"[{n}/{len(csv_paths)}] {symbol}: median_vol=None assumed={assumed_bps:.1f}bp "
              f"half_spread={half_spread_bps:.2f}bp", flush=True)

    if not rows:
        print("No rows collected -- nothing to report.")
        return

    df = pd.DataFrame(rows)
    report = render_report(df)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(report, encoding="utf-8")
    print(f"\nWrote {a.out}")


TIER_ORDER = (">=$10M (5bp)", "$1M-$10M (20bp)", "<$1M (50bp)")

# The two floor candidates docs/system-redesign-2026-10.md's P1e floor-candidate sweep is actually
# expected to test (see docs/p1d_universe_datasets.md §4, "No floor-candidate sweep ($1M / $100k)").
FLOOR_CANDIDATES_USD: tuple[float, ...] = (1_000_000.0, 100_000.0)

# Informational-only comparison point for render_floor_reachable_section below -- the EXISTING
# flat preset this universe work is replacing (COST_PRESETS["kraken_T3_taker"].slippage), not a
# proposed new tier. The frozen a-priori tiers (TIERS_USD_VOLUME_TO_SLIPPAGE in tiered_slippage.py)
# remain the actual gate regardless of what this comparison shows -- design doc §4.4 rule 3:
# "report, don't silently adjust."
FLAT_PRESET_BPS = 5.0


def render_floor_reachable_section(df: pd.DataFrame, floors: tuple[float, ...] = FLOOR_CANDIDATES_USD) -> str:
    """Architect review fix (P1d fix pass): the headline `## By tier` numbers above are computed
    over ALL checked symbols, including a long tail of `cancel_only`/near-zero-volume symbols that
    NO floor candidate would ever actually admit into a gate run -- that tail is exactly what
    dominated the original "<$1M (50bp): 25.4% exceeding" headline. This section restricts the
    same exceeding-rate computation to each floor candidate's own REACHABLE population: symbols
    whose OWN calibration-snapshot trailing 30-day median $ volume clears that floor (i.e. exactly
    the population a `min_median_quote_volume_usd` gate at that floor would actually let into the
    universe, among the symbols this calibration check has realized-spread data for).

    Also reports an INFORMATIONAL-ONLY flat-5bp comparison (`FLAT_PRESET_BPS`, the existing
    `kraken_T3_taker` preset this universe work replaces) alongside the tiered-assumption exceeding
    rate -- NOT a proposed tier change, just a check of how much stricter a flat 5bp assumption
    would have been for this same reachable population. This surfaces a real finding: at least two
    static-16 symbols (ALGO, XTZ) fall into a worse tier under volume-based tiering (20bp, 50bp)
    than their realized half-spread would justify (~3.1bp, ~3.9bp -- both comfortably under even
    the flat 5bp preset), which matters because a P1e gate comparison whose CI already sits close
    to zero can be moved by a few bp of excess assumed cost per trade on symbols already in the
    baseline."""
    lines = [
        "\n## Restricted to floor-candidate-reachable populations (P1d fix pass)\n",
        "The `## By tier` table above is computed over every symbol this check collected a real "
        "order book for -- including a long tail of `cancel_only`/near-zero-volume symbols that no "
        "floor candidate would ever actually admit into a gate run. Restricting to each floor "
        "candidate's own reachable population (symbols whose own calibration-snapshot trailing "
        "30-day median $ volume clears that floor) shows a very different picture:\n",
        "| Floor candidate | n reachable | median half-spread bp | p90 half-spread bp | max half-spread bp "
        "| n exceeding TIERED assumption | % exceeding tiered | n exceeding flat 5bp (informational) "
        "| % exceeding flat 5bp |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for floor in floors:
        sub = df[df["median_quote_volume_usd"].notna() & (df["median_quote_volume_usd"] >= floor)]
        if sub.empty:
            lines.append(f"| >=${floor:,.0f} | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |")
            continue
        tiered_exceeding = sub[sub["half_spread_bps"] > sub["assumed_bps"]]
        flat_exceeding = sub[sub["half_spread_bps"] > FLAT_PRESET_BPS]
        lines.append(
            f"| >=${floor:,.0f} | {len(sub)} | {sub['half_spread_bps'].median():.2f} | "
            f"{sub['half_spread_bps'].quantile(0.9):.2f} | {sub['half_spread_bps'].max():.2f} | "
            f"{len(tiered_exceeding)} | {len(tiered_exceeding) / len(sub) * 100:.1f}% | "
            f"{len(flat_exceeding)} | {len(flat_exceeding) / len(sub) * 100:.1f}% |"
        )
    lines.append(
        "\n`% exceeding flat 5bp` is informational only -- it shows how much stricter the OLD flat "
        "`kraken_T3_taker` preset (5bp) would have been for this reachable population, NOT a "
        "proposed tier change; the frozen a-priori tiers above remain the actual gate (design doc "
        "§4.4 rule 3: report, don't silently adjust).\n"
    )
    # ALGO and XTZ specifically (not a generic "big gap" scan over all of static-16, several of
    # which also show a gap -- these two are flagged because they sit right at the boundary a
    # floor-candidate choice would actually move: ALGO's own volume is close to the $1M floor
    # candidate, XTZ's is close to a $100k one, each far below the ORIGINAL flat `kraken_T3_taker`
    # preset's implicit "this coin is a liquid major" assumption). Reported with real, verified
    # numbers from this same per-symbol data, not a guessed figure.
    boundary_watch = ["ALGO", "XTZ"]
    boundary_lines = []
    for base in boundary_watch:
        row = df[df["symbol"] == f"{base}/USD"]
        if row.empty:
            continue
        r = row.iloc[0]
        boundary_lines.append(
            f"{base} (assumed {r['assumed_bps']:.0f}bp under its current `{r['tier']}` tier vs "
            f"realized half-spread {r['half_spread_bps']:.2f}bp -- comfortably inside even the "
            f"flat {FLAT_PRESET_BPS:.0f}bp preset)"
        )
    if boundary_lines:
        lines.append(
            "\n**Flag:** the frozen tiers may be quite conservative for some static-16 symbols whose "
            f"own trailing volume sits close to a floor-candidate boundary: {'; '.join(boundary_lines)}. "
            "Reported, not auto-adjusted -- but worth knowing for a P1e gate comparison whose CI "
            "already sits close to zero, since a few bp of excess assumed cost per trade on an "
            "EXISTING static-16 symbol can matter more than the same excess on a newly-added one.\n"
        )
    return "\n".join(lines)


def render_report(df: pd.DataFrame) -> str:
    """Builds the calibration markdown report from a per-symbol DataFrame (columns: symbol,
    median_quote_volume_usd, tier, assumed_bps, half_spread_bps, full_spread_bps, gap_bps). Pulled
    out of main() so the exact same rendering logic can also regenerate the report from an
    already-collected DataFrame (e.g. re-parsed from a previous run's per-symbol table) without
    re-fetching live order books -- see the P1d report for how this was actually used.

    Reports BOTH a median-based flag (the original rule-3 check) and a TAIL-risk flag (what
    fraction of a tier's symbols have their realized half-spread exceed that tier's assumed bp at
    all, not just on median) -- the median check alone can hide a long illiquid tail within a
    tier, which is exactly what this dataset's <$1M tier turned out to have (see the P1d report)."""
    lines = [
        "# Tiered slippage calibration check (P1d)\n",
        f"{len(df)} symbol(s) checked against live Kraken order books "
        f"({pd.Timestamp.now('UTC').isoformat()}).\n",
        "Tiers are PRE-REGISTERED and FROZEN regardless of this comparison (design doc §4.4 rule "
        "3: report, don't silently adjust). `assumed_bps` is the frozen tier's one-side slippage "
        "assumption; `half_spread_bps` ((ask-mid)/mid) is the more directly comparable realized "
        "number (crossing the spread to fill costs roughly half the full spread); `full_spread_bps` "
        "is also reported since it's the more commonly quoted number.\n",
        "## By tier\n",
        "| Tier | n | assumed bp | median half-spread bp | p90 half-spread bp | max half-spread bp "
        "| n exceeding assumed | % exceeding assumed |",
        "|---|---|---|---|---|---|---|---|",
    ]
    tier_exceed_frac: dict[str, float] = {}
    for tier in TIER_ORDER:
        sub = df[df["tier"] == tier]
        if sub.empty:
            continue
        exceeding = sub[sub["half_spread_bps"] > sub["assumed_bps"]]
        frac = len(exceeding) / len(sub)
        tier_exceed_frac[tier] = frac
        lines.append(
            f"| {tier} | {len(sub)} | {sub['assumed_bps'].median():.1f} | "
            f"{sub['half_spread_bps'].median():.2f} | {sub['half_spread_bps'].quantile(0.9):.2f} | "
            f"{sub['half_spread_bps'].max():.2f} | {len(exceeding)} | {frac * 100:.1f}% |"
        )
    lines.append(
        "\n## Flags (reported, not auto-adjusted -- design doc §4.4 rule 3: "
        "\"report, don't silently adjust the tiers\")\n"
    )
    any_flag = False
    for tier in TIER_ORDER:
        sub = df[df["tier"] == tier]
        if sub.empty:
            continue
        if sub["half_spread_bps"].median() > sub["assumed_bps"].median():
            any_flag = True
            lines.append(
                f"- **{tier}** (median check): median realized half-spread "
                f"({sub['half_spread_bps'].median():.2f}bp) exceeds the assumed tier "
                f"({sub['assumed_bps'].median():.1f}bp) -- this tier may be too tight overall.\n"
            )
        frac = tier_exceed_frac.get(tier, 0.0)
        if frac > 0.10:  # more than 10% of this tier's symbols individually exceed the assumed bp
            any_flag = True
            lines.append(
                f"- **{tier}** (tail check): {frac * 100:.1f}% of symbols in this tier have a "
                f"realized half-spread ABOVE the assumed {sub['assumed_bps'].median():.0f}bp "
                f"(median is fine -- this is a long-tail risk within the tier, not a median "
                f"miscalibration). Symbols this illiquid may need per-symbol handling beyond a "
                f"flat tier value; see the P1d report's worst offenders.\n"
            )
    if not any_flag:
        lines.append("- None.\n")
    lines.append(render_floor_reachable_section(df))
    lines.append("\n## Full per-symbol detail\n")
    lines.append("| Symbol | Median $ volume (30d) | Tier | Assumed bp | Half-spread bp | Full spread bp |")
    lines.append("|---|---|---|---|---|---|")
    for _, r in df.sort_values(["tier", "symbol"]).iterrows():
        mv = f"${r['median_quote_volume_usd']:,.0f}" if pd.notna(r["median_quote_volume_usd"]) else "n/a"
        lines.append(
            f"| {r['symbol']} | {mv} | {r['tier']} | {r['assumed_bps']:.1f} | "
            f"{r['half_spread_bps']:.2f} | {r['full_spread_bps']:.2f} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
