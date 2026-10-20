"""
validate_intraday_p0.py - quality checks for the P0 intraday dataset written by fetch_intraday_p0.py
(docs/intraday_research_plan.md phase P0).

  python strategy_pack_run/validate_intraday_p0.py --data-dir strategy_pack_run/data_intraday \
      --kraken --summary-out strategy_pack_run/data_intraday/coverage_summary.json

Checks (all pure functions on DataFrames/arrays so they are unit-testable without network):
  * gaps            - runs of missing bars (count, longest, dates). NOTE: Coinbase omits candles for
                      intervals with no trades, so a gap is "no trade OR API hole", not provably a hole.
  * sanity          - duplicate / unsorted timestamps, zero-volume bars, non-positive or NaN prices,
                      OHLC-ordering violations (high < max(o,c,l), low > min(o,c,h)).
  * resample        - 15m aggregated to 1h versus the separately fetched 1h bars.
  * kraken          - close-price difference (bp) Coinbase vs Kraken on the recent overlapping window.
  * market_wide_gaps- bars missing on (almost) every coin at once = exchange/API outage, not illiquidity.

Writes coverage_summary.json (small, committed) and prints a console report. SHA256SUMS.txt is written
with --sha-out. No API keys are used (public endpoints only).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

WATCHLIST = ["BTC", "ETH", "XRP", "SOL", "ADA", "DOGE", "AVAX", "LINK", "BNB", "LTC", "DOT", "ATOM", "ALGO", "BCH", "XTZ", "SHIB"]
TF_MS = {"15m": 900_000, "1h": 3_600_000}
KRAKEN_MEDIAN_FLAG_BP = 10.0
KRAKEN_P99_FLAG_BP = 100.0


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path)


# ---------------------------------------------------------------- gaps
def find_gap_runs(ts_ms: np.ndarray, tf_ms: int) -> list[tuple[int, int]]:
    """(first_missing_bar_open_ms, n_missing_bars) for every run of missing bars between consecutive rows.
    Input must be sorted ascending (duplicates produce delta 0 and are ignored here; sanity() reports them)."""
    ts = np.asarray(ts_ms, dtype=np.int64)
    if len(ts) < 2:
        return []
    d = np.diff(ts)
    idx = np.nonzero(d > tf_ms)[0]
    return [(int(ts[i] + tf_ms), int(d[i] // tf_ms - 1)) for i in idx]


def gap_stats(ts_ms: np.ndarray, tf_ms: int, top_n: int = 5) -> dict:
    ts = np.asarray(ts_ms, dtype=np.int64)
    runs = find_gap_runs(ts, tf_ms)
    n_bars = len(ts)
    span_bars = int((ts[-1] - ts[0]) // tf_ms) + 1 if n_bars else 0
    missing = sum(n for _, n in runs)
    hours = lambda n: n * tf_ms / 3_600_000  # noqa: E731
    longest = max(runs, key=lambda r: r[1]) if runs else None
    top = sorted(runs, key=lambda r: -r[1])[:top_n]
    return {
        "bars": n_bars,
        "expected_bars_in_span": span_bars,
        "missing_bars": missing,
        "missing_pct": round(100.0 * missing / span_bars, 3) if span_bars else 0.0,
        "gap_runs": len(runs),
        "gap_runs_ge_1h": sum(1 for _, n in runs if hours(n) >= 1),
        "gap_runs_ge_6h": sum(1 for _, n in runs if hours(n) >= 6),
        "gap_runs_ge_24h": sum(1 for _, n in runs if hours(n) >= 24),
        "longest_gap_bars": longest[1] if longest else 0,
        "longest_gap_hours": round(hours(longest[1]), 2) if longest else 0.0,
        "longest_gap_start": _iso(longest[0]) if longest else None,
        "top_gaps": [{"start": _iso(s), "missing_bars": n, "hours": round(hours(n), 2)} for s, n in top],
    }


def yearly_coverage(ts_ms: np.ndarray, tf_ms: int) -> dict[str, dict]:
    """Per calendar year (UTC): bars present vs bars expected between max(first bar, Jan 1) and
    min(last bar, Dec 31). Expected is capped to the coin's own span so listing year isn't penalised
    for pre-listing time."""
    ts = np.asarray(ts_ms, dtype=np.int64)
    if len(ts) == 0:
        return {}
    first, last = int(ts[0]), int(ts[-1])
    years = pd.to_datetime(ts, unit="ms", utc=True).year
    out = {}
    for y in sorted(set(years)):
        y_start = int(datetime(int(y), 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        y_end = int(datetime(int(y) + 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000) - tf_ms
        lo, hi = max(first, y_start), min(last, y_end)
        expected = int((hi - lo) // tf_ms) + 1
        present = int((years == y).sum())
        out[str(int(y))] = {"bars": present, "expected": expected, "coverage_pct": round(100.0 * present / expected, 2) if expected > 0 else None}
    return out


# ---------------------------------------------------------------- sanity
def sanity_report(df: pd.DataFrame) -> dict:
    ts = df["timestamp"].to_numpy(dtype=np.int64)
    o, h, l, c, v = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume"))
    price_cols = np.vstack([o, h, l, c])
    nan_rows = int(np.isnan(price_cols).any(axis=0).sum() + np.isnan(v).sum())
    nonpos = int((np.nan_to_num(price_cols, nan=1.0) <= 0).any(axis=0).sum())
    with np.errstate(invalid="ignore"):
        high_bad = (h < np.maximum(o, np.maximum(c, l)) - 1e-12 * np.abs(h))
        low_bad = (l > np.minimum(o, np.minimum(c, h)) + 1e-12 * np.abs(l))
    bad_idx = np.nonzero(high_bad | low_bad)[0]
    return {
        "duplicate_timestamps": int(len(ts) - len(np.unique(ts))),
        "unsorted": bool((np.diff(ts) < 0).any()) if len(ts) > 1 else False,
        "zero_volume_bars": int((v == 0).sum()),
        "nan_values": nan_rows,
        "nonpositive_price_bars": nonpos,
        "high_below_open_close_low": int(high_bad.sum()),
        "low_above_open_close_high": int(low_bad.sum()),
        "ohlc_violation_examples": [_iso(int(ts[i])) for i in bad_idx[:3]],
        "flat_bars_pct": round(100.0 * float(((h == l) & (v >= 0)).mean()), 3) if len(ts) else 0.0,
    }


# ---------------------------------------------------------------- resample consistency
def resample_consistency(df15: pd.DataFrame, df1h: pd.DataFrame, rel_tol: float = 1e-9, vol_rel_tol: float = 1e-6) -> dict:
    """Aggregate 15m -> 1h and compare with the independently fetched 1h bars.

    Complete hours (all 4 quarter-bars present): open/high/low/close/volume must agree. Partial hours
    (1-3 quarter-bars present, because Coinbase omits no-trade quarters): only containment can be checked
    (1h high >= max 15m high, 1h low <= min 15m low); 1h volume >= sum of present quarters."""
    a = df15.copy()
    a["hour"] = a["timestamp"] // 3_600_000 * 3_600_000
    g = a.groupby("hour", sort=True).agg(n=("timestamp", "size"), o=("open", "first"), h=("high", "max"),
                                          l=("low", "min"), c=("close", "last"), v=("volume", "sum"))
    b = df1h.set_index("timestamp")
    joined = g.join(b, how="outer")
    in_both = joined.dropna(subset=["n", "open"])
    only_15m = int(joined["open"].isna().sum())
    only_1h = int(joined["n"].isna().sum())

    def close(x, y, tol):
        return np.abs(x - y) <= tol * np.maximum(np.abs(y), 1e-12)

    full = in_both[in_both["n"] == 4]
    part = in_both[in_both["n"] < 4]
    res = {
        "hours_compared_complete": int(len(full)),
        "hours_compared_partial": int(len(part)),
        "hours_only_in_15m": only_15m,
        "hours_only_in_1h": only_1h,
    }
    for name, a_col, b_col, tol in (("open", "o", "open", rel_tol), ("high", "h", "high", rel_tol),
                                    ("low", "l", "low", rel_tol), ("close", "c", "close", rel_tol),
                                    ("volume", "v", "volume", vol_rel_tol)):
        bad = ~close(full[a_col], full[b_col], tol)
        res[f"complete_mismatch_{name}"] = int(bad.sum())
        if name == "volume" and len(full):
            rel = (np.abs(full[a_col] - full[b_col]) / np.maximum(full[b_col].abs(), 1e-12))
            res["complete_volume_max_rel_diff"] = float(rel.max())
    any_bad = np.zeros(len(full), dtype=bool)
    for a_col, b_col, tol in (("o", "open", rel_tol), ("h", "high", rel_tol), ("l", "low", rel_tol), ("c", "close", rel_tol)):
        any_bad |= ~close(full[a_col], full[b_col], tol).to_numpy()
    res["complete_hours_with_any_ohlc_mismatch"] = int(any_bad.sum())
    res["complete_mismatch_examples"] = [_iso(int(t)) for t in full.index[any_bad][:3]]
    if len(full):
        # How big are the disagreements, and when? (bp of the fetched-1h value, mismatching hours only)
        for name, a_col, b_col in (("open", "o", "open"), ("close", "c", "close")):
            bp = np.abs((full[a_col] / full[b_col] - 1.0) * 1e4)[any_bad]
            if len(bp):
                res[f"complete_{name}_abs_bp_p50_p99_max"] = [round(float(np.percentile(bp, 50)), 2),
                                                               round(float(np.percentile(bp, 99)), 2), round(float(bp.max()), 2)]
        years = pd.to_datetime(full.index.to_numpy(), unit="ms", utc=True).year
        by_year = {}
        for y in sorted(set(years)):
            m = years == y
            by_year[str(int(y))] = {"hours": int(m.sum()), "ohlc_mismatch_pct": round(100.0 * float(any_bad[m].mean()), 2)}
        res["complete_ohlc_mismatch_by_year"] = by_year
    res["partial_high_containment_violations"] = int((part["high"] < part["h"] * (1 - rel_tol)).sum())
    res["partial_low_containment_violations"] = int((part["low"] > part["l"] * (1 + rel_tol)).sum())
    res["partial_volume_below_sum_of_quarters"] = int((part["volume"] < part["v"] * (1 - vol_rel_tol)).sum())
    if len(part):
        # In a partial hour the 1h bar's open/close may legitimately differ from the 15m-derived ones
        # (the missing quarter-bar may hold the first/last trade). Count how often anyway.
        res["partial_hours_open_or_close_differs"] = int((~close(part["o"], part["open"], rel_tol) | ~close(part["c"], part["close"], rel_tol)).sum())
    return res


def daily_consistency(intra: pd.DataFrame, tf: str, daily: pd.DataFrame, rel_tol: float = 1e-6) -> dict:
    """Third-party tiebreaker for 15m-vs-1h disagreements: aggregate an intraday feed to UTC days (only days with
    every bar present) and compare close/high/low/volume with the independently fetched daily bars
    (data_long/<BASE>_USD_1d.csv). Returns per-year mismatch rates (share of complete days whose value differs
    by more than rel_tol)."""
    n_full = 86_400_000 // TF_MS[tf]
    a = intra.copy()
    a["day"] = a["timestamp"] // 86_400_000 * 86_400_000
    g = a.groupby("day").agg(n=("timestamp", "size"), c=("close", "last"), hi=("high", "max"), lo=("low", "min"), v=("volume", "sum"))
    j = g[g["n"] == n_full].join(daily.set_index("timestamp"), how="inner")
    if j.empty:
        return {}
    yrs = pd.to_datetime(j.index.to_numpy(), unit="ms", utc=True).year
    bad = {
        "close": np.abs(j["c"] / j["close"] - 1) > rel_tol,
        "high": np.abs(j["hi"] / j["high"] - 1) > rel_tol,
        "low": np.abs(j["lo"] / j["low"] - 1) > rel_tol,
        "volume": np.abs(j["v"] / j["volume"] - 1) > rel_tol,
    }
    out = {}
    for y in sorted(set(yrs)):
        m = yrs == y
        out[str(int(y))] = {"days": int(m.sum()), **{f"{k}_mismatch_pct": round(100.0 * float(b.to_numpy()[m].mean()), 2) for k, b in bad.items()}}
    return out


# ---------------------------------------------------------------- cross-venue
def bp_diff(a_close: np.ndarray, b_close: np.ndarray) -> np.ndarray:
    """Signed difference of a vs b in basis points of b."""
    a = np.asarray(a_close, dtype=float)
    b = np.asarray(b_close, dtype=float)
    return (a / b - 1.0) * 1e4


def cross_check_stats(cb: pd.DataFrame, kr: pd.DataFrame) -> dict:
    """Coinbase vs Kraken close-price difference on bars present in both. Flags per the P0 brief:
    median |diff| > 10bp or 99th percentile |diff| > 100bp."""
    m = cb.merge(kr, on="timestamp", suffixes=("_cb", "_kr"))
    if m.empty:
        return {"overlap_bars": 0, "flag": None, "note": "no overlapping bars"}
    bp = bp_diff(m["close_cb"].to_numpy(), m["close_kr"].to_numpy())
    ab = np.abs(bp)
    vol_ratio = (m["volume_cb"] / m["volume_kr"].replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
    med, p99 = float(np.median(ab)), float(np.percentile(ab, 99))
    reasons = []
    if med > KRAKEN_MEDIAN_FLAG_BP:
        reasons.append(f"median |diff| {med:.1f}bp > {KRAKEN_MEDIAN_FLAG_BP:.0f}bp")
    if p99 > KRAKEN_P99_FLAG_BP:
        reasons.append(f"p99 |diff| {p99:.1f}bp > {KRAKEN_P99_FLAG_BP:.0f}bp")
    return {
        "overlap_bars": int(len(m)),
        "window_start": _iso(int(m["timestamp"].min())),
        "window_end": _iso(int(m["timestamp"].max())),
        "median_abs_bp": round(med, 3),
        "p90_abs_bp": round(float(np.percentile(ab, 90)), 3),
        "p99_abs_bp": round(p99, 3),
        "max_abs_bp": round(float(ab.max()), 3),
        "mean_signed_bp": round(float(bp.mean()), 3),
        "median_volume_ratio_cb_over_kr": round(float(np.nanmedian(vol_ratio)), 3) if vol_ratio.notna().any() else None,
        "flag": "; ".join(reasons) if reasons else None,
    }


# ---------------------------------------------------------------- market-wide gaps
def market_wide_gaps(missing_sets: dict[str, set[int]], tf_ms: int, min_coins: int, min_run_bars: int) -> list[dict]:
    """Bars missing on >= min_coins coins simultaneously (each coin's own missing-bar timestamps in
    `missing_sets`), merged into runs of >= min_run_bars consecutive bars. Identifies exchange/API
    outages as opposed to coin-specific illiquidity."""
    counts: dict[int, int] = {}
    for s in missing_sets.values():
        for t in s:
            counts[t] = counts.get(t, 0) + 1
    ts = sorted(t for t, c in counts.items() if c >= min_coins)
    runs, start, prev = [], None, None
    for t in ts:
        if start is None:
            start = prev = t
        elif t - prev == tf_ms:
            prev = t
        else:
            runs.append((start, prev))
            start = prev = t
    if start is not None:
        runs.append((start, prev))
    out = []
    for s, e in runs:
        n = (e - s) // tf_ms + 1
        if n >= min_run_bars:
            out.append({"start": _iso(s), "end": _iso(e), "bars": int(n), "hours": round(n * tf_ms / 3_600_000, 2),
                        "min_coins_missing": min(counts[t] for t in range(s, e + tf_ms, tf_ms))})
    return out


def missing_timestamps(ts_ms: np.ndarray, tf_ms: int, lo: int, hi: int) -> set[int]:
    """Every bar-open timestamp in [lo, hi] (aligned to tf) that is absent from ts_ms."""
    present = set(int(t) for t in ts_ms)
    return {t for t in range(lo // tf_ms * tf_ms, hi + 1, tf_ms) if t not in present}


# ---------------------------------------------------------------- checksums
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_sha256sums(files: list[Path], base: Path, out: Path) -> None:
    lines = [f"{sha256_file(p)}  {p.relative_to(base).as_posix()}" for p in sorted(files)]
    out.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------- Kraken fetch (network; not unit-tested)
KRAKEN_TF = {"15m": "15m", "1h": "1h"}


def fetch_kraken_recent(base: str, tf: str, quote: str = "USD") -> pd.DataFrame | None:
    """Last <=720 CLOSED Kraken bars for base/quote, or None if Kraken doesn't list the pair."""
    import ccxt
    ex = ccxt.kraken({"enableRateLimit": True})
    ex.session.trust_env = True
    sym = f"{base}/{quote}"
    if not hasattr(ex, "_markets_loaded"):
        ex.load_markets()
        ex._markets_loaded = True
    if sym not in ex.markets:
        return None
    raw = ex.fetch_ohlcv(sym, KRAKEN_TF[tf], limit=720)
    now_ms = ex.milliseconds()
    tf_ms = TF_MS[tf]
    rows = [r for r in raw if r[0] + tf_ms <= now_ms]
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])


# ---------------------------------------------------------------- driver
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="strategy_pack_run/data_intraday")
    ap.add_argument("--quote", default="USD")
    ap.add_argument("--daily-dir", default="strategy_pack_run/data_long", help="daily CSVs used as a tiebreaker (optional)")
    ap.add_argument("--kraken", action="store_true", help="also fetch recent Kraken bars and cross-check closes")
    ap.add_argument("--summary-out", default=None)
    ap.add_argument("--sha-out", default=None)
    a = ap.parse_args()
    data_dir = Path(a.data_dir)

    summary: dict = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "source": "Coinbase Exchange public candles (api.exchange.coinbase.com), USD quote",
                     "coins": {}, "market_wide_gaps_15m": [], "market_wide_gaps_1h": []}
    frames: dict[tuple[str, str], pd.DataFrame] = {}
    files: list[Path] = []
    for base in WATCHLIST:
        entry: dict = {}
        for tf in ("1h", "15m"):
            p = data_dir / tf / f"{base}_{a.quote}_{tf}.csv"
            if not p.exists():
                entry[tf] = {"status": "missing"}
                continue
            files.append(p)
            df = load_csv(p)
            frames[(base, tf)] = df
            ts = df["timestamp"].to_numpy(dtype=np.int64)
            e = {"first_bar": _iso(int(ts[0])), "last_bar": _iso(int(ts[-1]))}
            e.update(gap_stats(ts, TF_MS[tf]))
            e["by_year"] = yearly_coverage(ts, TF_MS[tf])
            e["sanity"] = sanity_report(df)
            entry[tf] = e
        if (base, "15m") in frames and (base, "1h") in frames:
            entry["resample_15m_vs_1h"] = resample_consistency(frames[(base, "15m")], frames[(base, "1h")])
        daily_path = Path(a.daily_dir) / f"{base}_{a.quote}_1d.csv"
        if daily_path.exists():
            daily = load_csv(daily_path)
            for tf in ("1h", "15m"):
                if (base, tf) in frames:
                    entry[tf]["vs_daily"] = daily_consistency(frames[(base, tf)], tf, daily)
        summary["coins"][base] = entry
        print(f"{base}: " + ", ".join(f"{tf} {entry[tf].get('bars', 'NA')} bars ({entry[tf].get('first_bar', '')[:10]})" for tf in ("1h", "15m")), flush=True)

    # market-wide outages: window common to all coins that have started (use BTC span; coins not yet
    # listed are excluded from the denominator by only counting coins whose data covers the bar).
    for tf in ("15m", "1h"):
        sets, spans = {}, {}
        for base in WATCHLIST:
            if (base, tf) in frames:
                ts = frames[(base, tf)]["timestamp"].to_numpy(dtype=np.int64)
                spans[base] = (int(ts[0]), int(ts[-1]))
        if not spans:
            continue
        # only the last ~3 years keep this tractable and most coins listed; BNB (late listing) is skipped
        lo = int(datetime(2022, 10, 20, tzinfo=timezone.utc).timestamp() * 1000)
        hi = max(e for _, e in spans.values())
        core = [b for b in spans if spans[b][0] <= lo and b != "BNB"]
        for b in core:
            ts = frames[(b, tf)]["timestamp"].to_numpy(dtype=np.int64)
            sets[b] = missing_timestamps(ts, TF_MS[tf], lo, hi)
        min_coins = max(2, int(0.75 * len(core)))
        summary[f"market_wide_gaps_{tf}"] = market_wide_gaps(sets, TF_MS[tf], min_coins, 2 if tf == "15m" else 1)
        summary[f"market_wide_gaps_{tf}_params"] = {"window_start": _iso(lo), "coins": core, "min_coins_missing": min_coins}

    if a.kraken:
        kdir = data_dir / "_kraken"
        kdir.mkdir(parents=True, exist_ok=True)
        for base in WATCHLIST:
            for tf in ("1h", "15m"):
                if (base, tf) not in frames:
                    continue
                kr = fetch_kraken_recent(base, tf, a.quote)
                if kr is None:
                    res = {"overlap_bars": 0, "flag": None, "note": f"Kraken has no {base}/{a.quote} market"}
                else:
                    kr.to_csv(kdir / f"{base}_{a.quote}_{tf}_kraken.csv", index=False)
                    res = cross_check_stats(frames[(base, tf)], kr)
                    res["kraken_bars_fetched"] = int(len(kr))
                summary["coins"][base].setdefault("kraken_cross_check", {})[tf] = res
                print(f"kraken {base} {tf}: {res}", flush=True)

    if a.summary_out:
        Path(a.summary_out).write_text(json.dumps(summary, indent=1) + "\n")
        print(f"wrote {a.summary_out}")
    if a.sha_out:
        write_sha256sums(files, data_dir, Path(a.sha_out))
        print(f"wrote {a.sha_out} ({len(files)} files)")


if __name__ == "__main__":
    main()
