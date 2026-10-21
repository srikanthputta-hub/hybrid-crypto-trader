"""intraday_data.py - data, snapshot guard, universes, masks and daily plumbing for the intraday
P2 pre-registration (docs/intraday_prereg.md §1, docs/intraday_harness_changes.md H1/H7).

Research code only. Nothing here imports or modifies src/ trading code beyond the pure indicator
functions in src/indicators.py. Every constant below is copied from docs/intraday_prereg.md and is
asserted against that document by test_intraday_constants.py (AT20); none of them is a CLI flag.

Conventions (prereg §1.1):
  * Every frame is indexed by bar CLOSE time (open time + timeframe), UTC.
  * Missing bars are never forward-filled or interpolated; gaps stay gaps.
  * Daily bars (`1h->1d`) and 4h bars (`1h->4h`) are built ONLY from native 1h, complete bins only.
  * A frame carries `attrs["source"]` in {"native_1h", "native_15m", "1h->4h", "1h->1d"}; the
    mixed-source guard (`assert_sources`) refuses to combine native 15m with any 1h-derived
    intraday frame in one run (daily inputs are allowed in a 15m run: they are daily-only inputs).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from resample_ohlcv import resample_ohlcv  # noqa: E402
from src.indicators import adx as _adx  # noqa: E402
from src.indicators import atr as _atr  # noqa: E402
from src.indicators import sma as _sma  # noqa: E402

# --------------------------------------------------------------------------- #
# Frozen constants (prereg §1.2, §1.3)
# --------------------------------------------------------------------------- #
DATA_ROOT_DEFAULT = "/home/user/hct-data"
DATA_ROOT_ENV = "HCT_INTRADAY_DATA"
COVERAGE_JSON = _THIS_DIR / "data_intraday" / "coverage_summary.json"
SHA256SUMS = _THIS_DIR / "data_intraday" / "SHA256SUMS.txt"

WINDOW_END = pd.Timestamp("2026-10-20 00:00", tz="UTC")
B_START = pd.Timestamp("2021-10-01 00:00", tz="UTC")
A_START = pd.Timestamp("2023-07-14 00:00", tz="UTC")
B_DAYS = 1845
A_DAYS = 1194

UNIVERSE_B = ("BTC", "ETH", "SOL", "ADA", "DOGE", "AVAX", "LINK", "LTC", "DOT", "ATOM", "ALGO", "BCH", "XTZ", "SHIB")
UNIVERSE_A = UNIVERSE_B + ("XRP",)
UNIVERSE_B_15M = tuple(c for c in UNIVERSE_B if c != "XTZ")
UNIVERSE_A_15M = tuple(c for c in UNIVERSE_A if c != "XTZ")
EXCLUDED_COINS = ("BNB",)

# XRP: only the continuous post-relisting segment is ever read (P0 tier D): bar OPEN time >= this.
XRP_CONTINUOUS_FROM = pd.Timestamp("2023-07-13 20:00", tz="UTC")

# 15m masks (prereg §1.2; P0 §6): UTC dates, inclusive, every coin except BTC/ETH.
MASK_RANGES_15M = (
    ("2023-03-04", "2023-03-04"),
    ("2024-08-13", "2024-09-03"),
    ("2025-01-01", "2025-04-30"),
)
MASK_EXEMPT_15M = ("BTC", "ETH")
MASK_WARMUP_EXTRA_BARS = 100  # the "+100" of t - (n + 100)

MIN_DAILY_BARS = 60          # universe.min_listing_age_days
WARMUP_EXTRA = 100           # EMA/Wilder series need >= 100 + their length bars
WILDER_LEN = 14              # ATR_D, ADX_D, ATR_TF length
REGIME_SMA = 200             # BTC daily SMA

TF_MINUTES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}
SOURCES = ("native_1h", "native_15m", "1h->4h", "1h->1d")


class SnapshotMismatch(RuntimeError):
    """Raised when a data file's snapshot prefix does not hash to the committed SHA256SUMS entry.
    A run that hits this is VOID (prereg §1.3, §6.7)."""


class MixedSourceError(RuntimeError):
    """Raised when native 15m and 1h-derived intraday frames are combined in one run."""


def data_root(root: str | os.PathLike | None = None) -> Path:
    """The intraday data root: explicit argument, else $HCT_INTRADAY_DATA, else the frozen default."""
    if root is not None:
        return Path(root)
    return Path(os.environ.get(DATA_ROOT_ENV, DATA_ROOT_DEFAULT))


def _tf_delta(tf: str) -> pd.Timedelta:
    return pd.Timedelta(minutes=TF_MINUTES[tf])


# --------------------------------------------------------------------------- #
# Snapshot verification (prefix rule)
# --------------------------------------------------------------------------- #
def _prefix_sha256(path: Path, n_rows: int) -> str:
    """SHA-256 of the header line plus the first `n_rows` data lines, byte-exact as stored."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for i in range(n_rows + 1):
            line = f.readline()
            if not line:
                raise SnapshotMismatch(f"{path}: only {i - 1} data rows, snapshot needs {n_rows}")
            h.update(line)
    return h.hexdigest()


def read_sha256sums(sha_file: str | os.PathLike = SHA256SUMS) -> dict[str, str]:
    out = {}
    for line in Path(sha_file).read_text().splitlines():
        if not line.strip():
            continue
        digest, rel = line.split(None, 1)
        out[rel.strip()] = digest.strip()
    return out


def verify_snapshot(
    root: str | os.PathLike | None = None,
    coverage_json: str | os.PathLike = COVERAGE_JSON,
    sha_file: str | os.PathLike = SHA256SUMS,
    files: list[str] | None = None,
) -> dict[str, str]:
    """Prefix rule (prereg §1.3): for each file used (`files` = relative paths like
    "1h/BTC_USD_1h.csv"; None = every file listed in SHA256SUMS), hash the header plus the first
    `coins[BASE][tf]["bars"]` data lines and compare with SHA256SUMS. Appended rows after the
    prefix are allowed. Any mismatch raises SnapshotMismatch. Returns {relpath: digest}."""
    root = data_root(root)
    cov = json.loads(Path(coverage_json).read_text())["coins"]
    sums = read_sha256sums(sha_file)
    rels = sorted(sums) if files is None else sorted(files)
    out = {}
    for rel in rels:
        if rel not in sums:
            raise SnapshotMismatch(f"{rel}: not listed in {sha_file}")
        tf, fname = rel.split("/", 1)
        base = fname.split("_")[0]
        n_rows = int(cov[base][tf]["bars"])
        path = root / rel
        if not path.exists():
            raise SnapshotMismatch(f"{path}: missing")
        got = _prefix_sha256(path, n_rows)
        if got != sums[rel]:
            raise SnapshotMismatch(f"{rel}: prefix sha256 {got} != committed {sums[rel]}")
        out[rel] = got
    return out


def files_for(universe: tuple[str, ...], tf: str) -> list[str]:
    """Relative paths of the files a run on `universe` at base tf `tf` reads. A 15m run also reads
    the native 1h files (daily inputs are aggregated from native 1h, prereg §1.1)."""
    rels = [f"1h/{c}_USD_1h.csv" for c in universe]
    if tf == "15m":
        rels += [f"15m/{c}_USD_15m.csv" for c in universe]
    if "BTC" not in universe:
        rels.append("1h/BTC_USD_1h.csv")
    return sorted(set(rels))


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #
def frame_from_open_indexed(raw: pd.DataFrame, tf: str, base: str, end: pd.Timestamp | None = WINDOW_END) -> pd.DataFrame:
    """Shared tail of the loader (also used by synthetic fixtures): `raw` has an open-time index
    and OHLCV columns. Applies the XRP segment rule, shifts to close time, cuts at `end`, tags
    the source."""
    if base in EXCLUDED_COINS:
        raise ValueError(f"{base} is excluded from every intraday run (prereg §1.2)")
    if tf not in ("1h", "15m"):
        raise ValueError(f"native tf must be 1h or 15m, got {tf!r}")
    df = raw[["open", "high", "low", "close", "volume"]].astype(float).copy()
    if base == "XRP":
        df = df[df.index >= XRP_CONTINUOUS_FROM]
    df.index = df.index + _tf_delta(tf)
    df.index.name = "close_time"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if end is not None:
        df = df[df.index <= end]
    df.attrs["source"] = f"native_{tf}"
    df.attrs["base"] = base
    return df


def load_native(base: str, tf: str, root: str | os.PathLike | None = None, end: pd.Timestamp | None = WINDOW_END) -> pd.DataFrame:
    """`<root>/<tf>/<BASE>_USD_<tf>.csv` (timestamp = bar open, epoch ms) -> close-time indexed
    float frame, same columns as backtest_live_bot.load_csv. No forward fill; gaps kept."""
    if base in EXCLUDED_COINS:
        raise ValueError(f"{base} is excluded from every intraday run (prereg §1.2)")
    path = data_root(root) / tf / f"{base}_USD_{tf}.csv"
    raw = pd.read_csv(path)
    open_time = pd.to_datetime(raw["timestamp"].astype("int64"), unit="ms", utc=True)
    raw = raw.set_index(open_time)
    return frame_from_open_indexed(raw, tf, base, end)


def _derive_from_1h(df_1h: pd.DataFrame, target_tf: str, source_tag: str) -> pd.DataFrame:
    if df_1h.attrs.get("source") != "native_1h":
        raise MixedSourceError(f"{target_tf} bars may only be built from native 1h, got {df_1h.attrs.get('source')!r}")
    open_indexed = df_1h.copy()
    open_indexed.index = open_indexed.index - _tf_delta("1h")
    out = resample_ohlcv(open_indexed, target_tf, source_minutes=60)
    out.index = out.index + _tf_delta(target_tf)
    out.index.name = "close_time"
    out.attrs["source"] = source_tag
    out.attrs["base"] = df_1h.attrs.get("base")
    return out


def daily_from_1h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Complete UTC days only (24 of 24 hourly bars), first/max/min/last/sum, close-time indexed
    (a day's bar closes at 00:00 UTC of the next day)."""
    return _derive_from_1h(df_1h, "1d", "1h->1d")


def four_hour_from_1h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Complete 4h bins (4 of 4 hourly bars) opening 00/04/08/12/16/20 UTC, close-time indexed."""
    return _derive_from_1h(df_1h, "4h", "1h->4h")


def assert_sources(run_tf: str, base_frames, decision_frames, daily_frames=None) -> None:
    """Mixed-source guard (H1; prereg §1.1). `run_tf` is the run's base timeframe."""
    def srcs(frames):
        vals = frames.values() if isinstance(frames, dict) else frames
        return [f.attrs.get("source") for f in vals]

    for s in srcs(base_frames) + srcs(decision_frames) + (srcs(daily_frames) if daily_frames is not None else []):
        if s not in SOURCES:
            raise MixedSourceError(f"frame without a recognised source tag: {s!r}")
    if run_tf == "15m":
        bad = [s for s in srcs(base_frames) + srcs(decision_frames) if s != "native_15m"]
        if bad:
            raise MixedSourceError(f"15m run fed non-15m intraday frames: {sorted(set(bad))}")
    elif run_tf == "1h":
        bad = [s for s in srcs(base_frames) if s != "native_1h"] + [
            s for s in srcs(decision_frames) if s not in ("native_1h", "1h->4h")
        ]
        if bad:
            raise MixedSourceError(f"1h run fed non-1h intraday frames: {sorted(set(bad))}")
    else:
        raise ValueError(f"run_tf must be 1h or 15m, got {run_tf!r}")
    if daily_frames is not None:
        bad = [s for s in srcs(daily_frames) if s != "1h->1d"]
        if bad:
            raise MixedSourceError(f"daily inputs must be aggregated from native 1h, got {sorted(set(bad))}")


# --------------------------------------------------------------------------- #
# Daily inputs and regime (H7)
# --------------------------------------------------------------------------- #
def btc_regime(daily_btc: pd.DataFrame, sma_len: int = REGIME_SMA) -> pd.Series:
    """True iff BTC's daily close > SMA(sma_len) of daily closes; False during warm-up. Indexed by
    daily CLOSE time (`research/v2_signals.regime_on` semantics)."""
    sma = _sma(daily_btc["close"], sma_len)
    return ((daily_btc["close"] > sma) & sma.notna()).astype(bool)


def align(series: pd.Series, index: pd.DatetimeIndex, fill=np.nan) -> pd.Series:
    """Latest COMPLETED daily value at each decision close: reindex with forward fill, so a
    decision closing exactly at 00:00 sees the day that closed at 00:00 (never the forming one)."""
    out = series.reindex(index, method="ffill")
    if fill is not np.nan:
        out = out.fillna(fill)
    return out


def daily_inputs(daily: pd.DataFrame) -> pd.DataFrame:
    """Per-coin daily columns, indexed by daily close time: ATR_D(14), ADX_D(14), daily close,
    atr_pct (= ATR_D / that day's close), the running count of complete daily bars, and the
    warm flags for the Wilder series (>= 100 + 14 bars)."""
    out = pd.DataFrame(index=daily.index)
    out["close_d"] = daily["close"]
    out["atr_d"] = _atr(daily, WILDER_LEN)
    out["adx_d"] = _adx(daily, WILDER_LEN)
    out["atr_pct"] = out["atr_d"] / out["close_d"]
    out["n_daily"] = np.arange(1, len(daily) + 1, dtype=float)
    out["quote_vol_d"] = (daily["close"] * daily["volume"]).astype(float)
    return out


def wilder_warm(n_bars, length: int = WILDER_LEN):
    return n_bars >= WARMUP_EXTRA + length


# --------------------------------------------------------------------------- #
# Masks (15m) and eligibility
# --------------------------------------------------------------------------- #
def mask_intervals_ns() -> np.ndarray:
    """[start, end) of every masked UTC day range, as int64 ns."""
    rows = []
    for a, b in MASK_RANGES_15M:
        s = pd.Timestamp(a, tz="UTC")
        e = pd.Timestamp(b, tz="UTC") + pd.Timedelta(days=1)
        rows.append((s.value, e.value))
    return np.array(rows, dtype=np.int64)


def is_masked_coin(coin: str) -> bool:
    return coin not in MASK_EXEMPT_15M


def bar_masked_15m(coin: str, close_times: pd.DatetimeIndex) -> np.ndarray:
    """True for a 15m bar of a masked coin whose span [close - 15m, close) lies in a masked day."""
    if not is_masked_coin(coin):
        return np.zeros(len(close_times), dtype=bool)
    # as_unit("ns"): pandas >= 2 indexes may be in us/ms resolution, and .asi8 returns that unit
    opens = (pd.DatetimeIndex(close_times) - _tf_delta("15m")).as_unit("ns").asi8
    out = np.zeros(len(opens), dtype=bool)
    for s, e in mask_intervals_ns():
        out |= (opens >= s) & (opens < e)
    return out


def entry_allowed_15m_mask(coin: str, decision_close_ns: np.ndarray, n: int, H: int) -> np.ndarray:
    """Vectorised prereg §1.2 rule for decision closes `t`: a masked coin may enter only if no
    masked 15m bar lies in [t - (n + 100) bars, t + H bars]. Implemented on calendar time as: the
    span from the open of the bar closing at t-(n+100)*15m (= t-(n+101)*15m) to the close of the
    bar that executes the time-stop exit (t+(H+1)*15m) must not intersect a masked day. This is
    the superset of both readings of the bar labels (close-time vs open-time), i.e. the more
    conservative one; it also keeps the time-stop exit fill bar itself out of every mask."""
    t = np.asarray(decision_close_ns, dtype=np.int64)
    if not is_masked_coin(coin):
        return np.ones(len(t), dtype=bool)
    step = _tf_delta("15m").value
    lo = t - (n + MASK_WARMUP_EXTRA_BARS + 1) * step
    hi = t + (H + 1) * step
    ok = np.ones(len(t), dtype=bool)
    for s, e in mask_intervals_ns():
        ok &= ~((lo < e) & (hi > s))
    return ok


def entry_allowed_15m(coin: str, t: pd.Timestamp, n: int, H: int) -> bool:
    return bool(entry_allowed_15m_mask(coin, np.array([pd.Timestamp(t).value]), n, H)[0])


def eligible(n_daily_bars, family_warm) -> np.ndarray:
    """Entry eligibility (prereg §1.2): >= 60 complete daily bars before t AND every indicator the
    family uses is warm (the family passes its own warm flag array)."""
    return (np.asarray(n_daily_bars, dtype=float) >= MIN_DAILY_BARS) & np.asarray(family_warm, dtype=bool)


# --------------------------------------------------------------------------- #
# Universe loading
# --------------------------------------------------------------------------- #
def load_universe(universe: tuple[str, ...], tf: str, root=None, end: pd.Timestamp = WINDOW_END) -> dict:
    """Loads, per coin, the native base frame (`tf`), native 1h, and the 1h->1d daily frame; plus
    BTC's daily frame for the regime. BNB never loaded. Returns a dict the runner consumes."""
    native_1h = {}
    for c in sorted(set(universe) | {"BTC"}):
        native_1h[c] = load_native(c, "1h", root, end)
    base = {c: (native_1h[c] if tf == "1h" else load_native(c, "15m", root, end)) for c in universe}
    daily = {c: daily_from_1h(native_1h[c]) for c in native_1h}
    return dict(tf=tf, universe=tuple(universe), native_1h=native_1h, base=base, daily=daily)


def cross_check_daily_vs_data_long(daily: pd.DataFrame, data_long_csv: str | os.PathLike,
                                   first_day=B_START, last_day=pd.Timestamp("2026-09-30 00:00", tz="UTC"),
                                   rtol: float = 1e-6) -> float:
    """Share of complete UTC days `first_day..last_day` (inclusive, by the day's own date) whose
    1h-aggregated close equals the data_long daily close within `rtol` (prereg §1.5 cross-check,
    AT5). Days absent from either side are not counted (complete days only)."""
    raw = pd.read_csv(data_long_csv)
    idx = pd.to_datetime(raw["timestamp"].astype("int64"), unit="ms", utc=True) + pd.Timedelta(days=1)
    ref = pd.Series(raw["close"].astype(float).to_numpy(), index=idx)
    ref = ref[~ref.index.duplicated(keep="last")]
    d = daily["close"]
    d = d[(d.index > first_day) & (d.index <= last_day + pd.Timedelta(days=1))]
    common = d.index.intersection(ref.index)
    if len(common) == 0:
        return float("nan")
    a, b = d.loc[common].to_numpy(), ref.loc[common].to_numpy()
    return float(np.mean(np.abs(a - b) <= rtol * np.abs(b)))
