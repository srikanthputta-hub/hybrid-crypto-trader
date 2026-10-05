"""backtest_live_bot.py - portfolio-level, walk-forward replay of the LIVE bot's OWN pipeline
(src/scanner.py -> src/signal_engine.py -> src/htf_filter.py -> src/trade_planner.py ->
src/risk_engine.py -> src/monitor.py), reused exactly as-is (read-only imports), against daily
OHLCV history instead of the live 1h feed.

THIS IS NOT A MODIFICATION OF engine.py's generic vectorized backtester -- that engine (and
strategies.py) evaluate two unrelated CANDIDATE strategies (trend_ensemble/tsmom) against a
different, simpler "enter/hold/rank" contract. This bot's actual pipeline is not vectorizable
the same way (the scanner runs five rule-based pattern detectors per symbol per bar, the risk
engine carries portfolio-wide state like cooldowns/daily-loss/exposure across the whole run,
and a plan's entry is a *zone* watched by src/monitor.py rather than an instant market order) --
so this module drives the real pipeline functions directly, one bar at a time, in chronological
lockstep across all symbols, mirroring how src/bot.py's run_cycle() drives them for one process
against one continuous portfolio.

Despite the different internal engine, the OUTPUT shape matches engine.py's Result (same
trades/equity/exposure/cfg/costs fields) specifically so engine.py's own generic, pure
summarize()/bootstrap_ci()/buy_hold() can be reused UNCHANGED (see run_live_bot_report.py).

METHODOLOGY NOTES / KNOWN SIMPLIFICATIONS (read before trusting the numbers)
-----------------------------------------------------------------------------
1. DAILY BARS, NOT THE LIVE 1h FEED. config/config.yaml runs market.timeframe: "1h"; only daily
   history this deep is available here. Every lookback/threshold below is the bot's REAL
   configured value, just evaluated once per day instead of once per hour -- this tests the
   pipeline's *logic*, not an exact replay of live 1h behavior. Two concrete distortions worth
   knowing about, not "bugs" in this harness:
     - risk.cooldown_minutes=60 is structurally non-binding here: consecutive evaluations of the
       same symbol are ~1440 minutes apart (one daily bar), so the cooldown gate can never
       actually block a trade in this replay, whereas live, 1h apart, it still gives some symbols
       a one-cycle gap.
     - risk.max_daily_loss_pct's baseline resets every time a new calendar date is seen
       (src/risk_engine.py's _refresh_daily_baseline); at one evaluation per day, that reset
       always happens right before the day's own check, so the daily-loss halt is also
       structurally non-binding in this replay (it's designed to compare against multiple
       intraday checks against one fixed baseline, which doesn't exist at daily resolution).
2. ENTRY/EXIT TIMING: a trigger is decided on a CLOSED bar's CLOSE (mirroring src/monitor.py's
   actual close-price-only checks -- no intrabar touches, unlike engine.py's chandelier-stop
   model) and FILLS/EXITS AT THE NEXT BAR'S OPEN (same next-bar-open, no-look-ahead discipline
   documented and tested in src/risk_counterfactual.py). This applies uniformly to FILL,
   DISCARD, STOP_LOSS and TAKE_PROFIT: even a DISCARD (which risks no money, so timing doesn't
   affect P&L) is deferred to the next bar's open purely so one consistent queue mechanism
   handles every trigger type.
3. A newly-built plan's entry_zone always contains the bar's own close price by construction
   (src/trade_planner.py centers the zone on max/min(level, current_price)), so a fresh PENDING
   plan is checked for an immediate FILL trigger the same bar it's created -- exactly like
   src/bot.py's "don't wait for the next tick" branch -- but, per (2) above, that FILL still only
   executes at the NEXT bar's open, never the planning bar's own close/open.
4. trade_planner.min_risk_reward (2.0) and risk.min_risk_reward (1.5) are DIFFERENT configured
   values. Because take_profit is solved to hit exactly trade_planner's target R:R, every plan's
   risk_reward comes out at ~2.0 by construction -- comfortably above risk_engine's 1.5 floor. In
   this replay (and, by the same arithmetic, live) the risk engine's own R:R gate is therefore
   never the reason a plan gets rejected; worth knowing when reading the risk-rejection mix.
5. SAME-BAR CROSS-SYMBOL CASH RACE: within one bar's close-decision pass, two different symbols'
   plans can both be approved by the risk engine off the same `cash_available` snapshot (neither
   has actually spent anything yet -- spending only happens at the next bar's open). If both then
   fill on the same next-bar open and jointly would overspend, this harness clips the second
   fill's spend to whatever cash remains rather than raising (src/portfolio.py's real
   apply_buy() would raise insufficient-cash). This mirrors a latent, pre-existing property of
   src/bot.py itself (symbols are approved in the same per-cycle loop before any of them have
   necessarily filled) -- not something introduced by this harness, and not something this
   harness's read-only mandate allows "fixing" in src/.
6. Only one active plan per symbol at a time (PENDING or OPEN blocks scanning for a new setup),
   matching src/bot.py's run_cycle exactly ("if record is not None: check monitor; else scan").
7. Position sizing, once a plan is approved, is fixed at approval time (the risk engine's
   max_quote_amount) -- not re-sized at fill time -- matching src/bot.py's plan_dict/
   approved_quote_amount flow exactly.
8. `entry_rank` is always 0 and `expo_at_entry` is computed the same way engine.py computes it
   (invested fraction of equity immediately after the fill) -- there's no rank-ordered capital
   queue here since, unlike engine.py's generic strategies, this pipeline only ever proposes one
   plan at a time per symbol with no competing candidates to rank on the same bar.
"""
from __future__ import annotations

import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from engine import Costs, EngineConfig, Result  # noqa: E402

from src import htf_filter, scanner, signal_engine, trade_planner  # noqa: E402
from src.indicators import add_indicators  # noqa: E402
from src.monitor import check_open, check_pending  # noqa: E402
from src.risk_engine import RiskEngine  # noqa: E402
from src.signal_engine import Signal  # noqa: E402
from src.trade_plan_store import PlanRecord  # noqa: E402

# Phase 2.0 (docs/system-redesign-2026-10.md §5.4): generalized from {"1d": 86400} so this
# harness can replay 15m/1h/4h data derived by resample_ohlcv.py, not just the original daily
# datasets. Same literal values as strategies.py's own TF_SECONDS (duplicated, not imported --
# this module is deliberately independent of engine.py/strategies.py's unrelated
# trend_ensemble/tsmom backtester, see the module docstring).
TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

# Point-in-time trailing-volume window for `rank_signals`' liquidity tie-break (P1c, see
# docs/system-redesign-2026-10.md §4.1/§4.4 rule 2). Same 30-day definition already named by
# `src.signal_engine.rank_signals`'s docstring (`median_quote_volume_30d`) and by the design
# doc's `universe.min_median_quote_volume_usd` -- this harness does not invent a new formula,
# it just computes the one that's already specified, since no universe/liquidity module exists
# yet to supply it (that lands in a later phase, §4.1's src/universe.py).
#
# This is a CALENDAR-day window, not a bar-count one -- at 1d resolution those are the same
# number (30 bars = 30 days), which is why a bare bar-count constant was correct until now.
# liquidity_window_bars() below converts it to the right bar count for any timeframe (Phase 2.0
# generalization: at 1h, 30 calendar days is 720 bars, not 30).
LIQUIDITY_WINDOW_DAYS = 30


def liquidity_window_bars(timeframe: str = "1d") -> int:
    """How many `timeframe` bars span LIQUIDITY_WINDOW_DAYS calendar days. 30 at "1d" (unchanged
    from before this function existed -- every daily-dataset report's numbers are byte-identical),
    720 at "1h", 180 at "4h", 2880 at "15m"."""
    return LIQUIDITY_WINDOW_DAYS * 86400 // TF_SECONDS[timeframe]


# Pure performance bound: every indicator column (ema/rsi/atr/adx) was already computed
# causally over each symbol's FULL history (see add_indicators -- all rolling/ewm, no
# look-ahead), and every scanner/htf_filter lookback actually used below (breakout_lookback=20,
# trend_confirm_bars+consolidation_bars=18, reversal_swing_lookback=10, htf slow_ema=26, plus a
# couple of bars of margin) is well under this. These lookbacks are all configured in BAR COUNTS,
# not calendar time, so this bound is already timeframe-agnostic -- 150 bars of margin is just as
# valid at 1h as at 1d; nothing here needed to change for Phase 2.0's 1h replay
# (docs/system-redesign-2026-10.md §5.4). Slicing a bounded tail therefore produces byte-identical
# detector output to passing the full growing history -- the same truncation-invariance property
# strategy_pack_run/test_pack.py's test_truncation_no_lookahead already checks for the
# strategy-pack strategies -- it just avoids re-materializing a thousands-of-rows DataFrame slice
# on every single bar.
SCAN_TAIL_BARS = 150


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #
def load_csv(path: str | Path, timeframe: str = "1d") -> pd.DataFrame:
    """Columns: timestamp (bar OPEN time, epoch ms or ISO), open, high, low, close, volume.
    Returns a float DataFrame indexed by bar CLOSE time (UTC) -- same convention as
    strategy_pack_run/strategies.py's load_csv, so dates line up with the existing reports.
    Despite the name, reads CSV or parquet based on `path`'s own suffix -- Phase 2.0's intraday
    dataset is parquet (docs/system-redesign-2026-10.md §5.4: "stored gitignored as parquet, not
    as CSV"; see resample_ohlcv.py), everything before it is CSV; both land in the same shape here.

    `timeframe` (Phase 2.0 generalization, default "1d" preserves every existing caller's
    behavior byte-for-byte): which TF_SECONDS entry to add to the file's bar-OPEN timestamps to
    get the bar-CLOSE index this harness actually walks on."""
    path = Path(path)
    raw = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
    ts = raw["timestamp"]
    open_time = (
        pd.to_datetime(ts, unit="ms", utc=True)
        if pd.api.types.is_numeric_dtype(ts)
        else pd.to_datetime(ts, utc=True)
    )
    df = raw[["open", "high", "low", "close", "volume"]].astype(float).copy()
    df.index = open_time + pd.Timedelta(seconds=TF_SECONDS[timeframe])
    df.index.name = "close_time"
    return df[~df.index.duplicated(keep="last")].sort_index()


def load_dir(data_dir: str | Path, timeframe: str = "1d") -> dict[str, pd.DataFrame]:
    """`timeframe` (Phase 2.0 generalization, default "1d" preserves every existing caller's
    behavior byte-for-byte): globs `*_{timeframe}.csv` instead of always `*_1d.csv`, so this can
    also load the 1h files resample_ohlcv.py derives from the new 15m Coinbase fetch, out of the
    SAME directory as the 1d/4h files (they don't collide, since the glob is suffix-specific).
    Also globs `*_{timeframe}.parquet` (Phase 2.0's actual on-disk format for the intraday
    dataset, see load_csv) -- a directory with only `.csv` files behaves exactly as before this
    was added."""
    out = {}
    paths = sorted(Path(data_dir).glob(f"*_{timeframe}.csv")) + sorted(Path(data_dir).glob(f"*_{timeframe}.parquet"))
    for p in paths:
        out[p.stem.split("_")[0].upper()] = load_csv(p, timeframe)
    return out


def trailing_quote_volume(enriched: pd.DataFrame, window: int = LIQUIDITY_WINDOW_DAYS) -> pd.Series:
    """Point-in-time trailing MEDIAN dollar volume (`close * volume`, `window`-bar median).

    Bar t's value only uses bars up to and including t (`min_periods=1`, no look-ahead) -- the
    same point-in-time discipline as every other lookback in this harness (see the module
    docstring's SCAN_TAIL_BARS note). Used only as `rank_signals`' liquidity tie-break input
    (`src.signal_engine.rank_signals`'s second argument); never affects sizing or costs.

    `window` is a BAR count, not a calendar-day count -- pass `liquidity_window_bars(timeframe)`
    for any non-daily timeframe (see that function's docstring); the bare LIQUIDITY_WINDOW_DAYS
    default here only means "30 bars" (= 30 calendar days at 1d, Phase 2.0 generalization note).
    """
    return (enriched["close"] * enriched["volume"]).rolling(window=window, min_periods=1).median()


# --------------------------------------------------------------------------- #
# Stage A: pure per-symbol signal detection (scanner -> signal_engine -> htf_filter)
# --------------------------------------------------------------------------- #
# How many trailing daily bars src/htf_filter.py's LIVE wrapper (daily_trend_confirmed) actually
# fetches, regardless of market.timeframe: `exchange.fetch_ohlcv_df(symbol, "1d", max(slow_ema + 5,
# 30))`. Mirrored here (not reimplemented as a different formula) so the point-in-time daily-trend
# check below sees the same bounded window live does, not the symbol's full daily history.
def _htf_fetch_bars(slow_ema: int) -> int:
    return max(slow_ema + 5, 30)


def compute_symbol_signals(
    enriched: pd.DataFrame,
    scanner_cfg: dict[str, Any],
    signal_cfg: dict[str, Any],
    htf_enabled: bool,
    fast_ema: int,
    slow_ema: int,
    tail: int = SCAN_TAIL_BARS,
    daily_closes: pd.Series | None = None,
) -> dict[pd.Timestamp, Signal]:
    """For every bar (on a trailing window ending at that bar -- no look-ahead), run the real
    scanner -> signal_engine -> (optional) HTF gate. Returns only the bars where a BULLISH,
    HTF-confirmed Signal exists -- i.e. exactly the bars src/bot.py would go on to call
    trade_planner.build_plan() for. The plan itself is built later, at simulate time (see
    simulate_portfolio), so the exact same code path also works for a time-shifted placebo
    signal (see placebo_run).

    `daily_closes` (Phase 2.0 generalization, docs/system-redesign-2026-10.md §5.4 -- default
    None preserves every existing daily-harness caller's behavior byte-for-byte): a close-time
    indexed Series of REAL daily closes for this symbol, separate from `enriched` (which is at
    whatever `market.timeframe` this replay uses). Why this matters: src/htf_filter.py's LIVE
    wrapper (daily_trend_confirmed) always fetches real "1d" candles no matter what
    market.timeframe is -- it is NOT the same frame the scanner runs on. The daily-only harness
    got away with reusing `window` (the scan-timeframe frame) for both, because at daily
    resolution those two frames are identical; at 1h they are NOT, and reusing the hourly window
    would silently check an hourly EMA cross and call it "the daily gate" -- a real behavior
    divergence from src/bot.py, not a harmless simplification. When `daily_closes` is given, the
    gate instead uses only daily bars whose own CLOSE TIME is at or before this bar's close time
    (`.loc[:as_of]` -- the forming daily bar is excluded, the same rule live's
    `fetch_ohlcv_df`/`market_data.py` apply), tailed to `_htf_fetch_bars(slow_ema)` bars, exactly
    matching what a live fetch at that moment would have returned."""
    n = len(enriched)
    signals: dict[pd.Timestamp, Signal] = {}
    htf_bars = _htf_fetch_bars(slow_ema)
    for i in range(3, n):
        window = enriched.iloc[max(0, i - tail + 1) : i + 1]
        candidates = scanner.scan(window, scanner_cfg)
        sig = signal_engine.evaluate(candidates, **signal_cfg)
        if sig is None or sig.direction != "bullish":
            continue
        if htf_enabled:
            if daily_closes is not None:
                as_of = enriched.index[i]
                eligible_daily = pd.DataFrame({"close": daily_closes.loc[:as_of].tail(htf_bars)})
                if not htf_filter.daily_trend_confirmed_from_df(eligible_daily, fast_ema, slow_ema):
                    continue
            elif not htf_filter.daily_trend_confirmed_from_df(window, fast_ema, slow_ema):
                continue
        signals[enriched.index[i]] = sig
    return signals


def build_enriched_and_signals(
    raw_frames: dict[str, pd.DataFrame],
    indicators_cfg: dict[str, Any],
    scanner_cfg: dict[str, Any],
    signal_cfg: dict[str, Any],
    htf_enabled: bool,
    daily_closes_by_symbol: dict[str, pd.Series] | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, dict[pd.Timestamp, Signal]], pd.DatetimeIndex]:
    """Computes indicators + Stage A signals per symbol on each symbol's own native (gap-free)
    history, then reindexes the enriched frames onto the shared global axis (union of all
    symbols' dates) -- late-listed coins get leading NaNs, exactly like engine.py's own
    `idx = pd.DatetimeIndex(sorted(set().union(...)))` / `.reindex(idx)` pattern.

    `daily_closes_by_symbol` (Phase 2.0 generalization, default None preserves every existing
    caller's behavior byte-for-byte): forwarded per-symbol to compute_symbol_signals as
    `daily_closes` -- see that function's docstring for why a sub-daily replay needs a real,
    separately-loaded daily series rather than reusing `enriched`."""
    fast_ema, slow_ema, rsi_period = (
        indicators_cfg["fast_ema"],
        indicators_cfg["slow_ema"],
        indicators_cfg["rsi_period"],
    )
    enriched_native: dict[str, pd.DataFrame] = {}
    signals_by_symbol: dict[str, dict[pd.Timestamp, Signal]] = {}
    for sym, raw in raw_frames.items():
        enr = add_indicators(raw, fast_ema, slow_ema, rsi_period)
        enriched_native[sym] = enr
        daily_closes = (daily_closes_by_symbol or {}).get(sym)
        signals_by_symbol[sym] = compute_symbol_signals(
            enr, scanner_cfg, signal_cfg, htf_enabled, fast_ema, slow_ema, daily_closes=daily_closes
        )

    global_idx = pd.DatetimeIndex(sorted(set().union(*[f.index for f in enriched_native.values()])))
    enriched_global = {sym: enr.reindex(global_idx) for sym, enr in enriched_native.items()}
    return enriched_global, signals_by_symbol, global_idx


# --------------------------------------------------------------------------- #
# Stage B: portfolio-level walk-forward simulation, driven by the REAL risk engine + monitor
# --------------------------------------------------------------------------- #
@dataclass
class _SymState:
    status: str = "FLAT"  # FLAT | PENDING | OPEN
    plan: dict[str, Any] | None = None
    queued: tuple[str, float | None] | None = None  # (action, fill_quote_amount) for next bar's open
    qty: float = 0.0
    entry_px: float = 0.0
    entry_fee: float = 0.0
    entry_idx: int = -1
    entry_expo: float = 0.0
    planned_risk_per_unit: float = 0.0


def _as_plan_record(symbol: str, status: str, plan: dict[str, Any]) -> PlanRecord:
    return PlanRecord(symbol=symbol, status=status, plan=plan)


def simulate_portfolio(
    enriched_global: dict[str, pd.DataFrame],
    signals_by_symbol: dict[str, dict[pd.Timestamp, Signal]],
    costs: Costs,
    risk_cfg: dict[str, Any],
    planner_cfg: dict[str, Any],
    start_equity: float,
    periods_per_year: float,
    project_root: Path,
    use_rank_order: bool = False,
    timeframe: str = "1d",
) -> Result:
    """Walks the shared global date axis one bar at a time, for every symbol in lockstep, with
    ONE RiskEngine instance and ONE cash/position pool for the whole run -- mirroring how
    src/bot.py runs a single continuous process against a single continuous portfolio. Accepts
    `signals_by_symbol` as an argument (rather than recomputing it) so the identical simulation
    code can be reused for both the real run and a time-shifted placebo run (see placebo_run).

    `timeframe` (Phase 2.0 generalization, default "1d" preserves every existing caller's
    behavior byte-for-byte): only used to pick the right bar count for `trailing_quote_volume`'s
    30-CALENDAR-day liquidity window when `use_rank_order=True` (see liquidity_window_bars) --
    every other part of this function is already timeframe-agnostic (it only ever counts bars,
    never calendar time).

    `use_rank_order` (default False, preserving every existing caller's behavior byte-for-byte --
    run_live_bot_report.py, hyperopt_param.py, every test in this file never pass it): when a
    bar has more than one FLAT symbol with a fresh signal competing for the same scarce
    new-entry slot (the GLOBAL `max_new_entries_per_window` cap in src/risk_engine.py -- the
    only place per-bar processing order can actually change which symbol wins; see
    docs/p1c_rank_signals_validation.md for why `max_total_exposure_pct` does NOT depend on this
    order), those symbols are visited in `src.signal_engine.rank_signals` order (highest
    engine_score first, tie-broken by `trailing_quote_volume` then symbol) instead of
    `sorted(symbols)`. Symbols without a fresh signal this bar (OPEN/PENDING monitor-only, or
    FLAT with nothing new) are visited afterward in the original `sorted(symbols)` order --
    their processing order never matters since they don't compete for anything."""
    symbols = sorted(enriched_global)  # fixed, deterministic per-bar processing order
    idx = next(iter(enriched_global.values())).index
    T = len(idx)
    liquidity_arr = (
        {
            s: trailing_quote_volume(enriched_global[s], window=liquidity_window_bars(timeframe)).to_numpy()
            for s in symbols
        }
        if use_rank_order
        else {}
    )

    risk_engine = RiskEngine(
        starting_cash_usd=risk_cfg["starting_cash_usd"],
        max_position_pct=risk_cfg["max_position_pct"],
        max_total_exposure_pct=risk_cfg["max_total_exposure_pct"],
        max_loss_per_trade_pct=risk_cfg["max_loss_per_trade_pct"],
        max_daily_loss_pct=risk_cfg["max_daily_loss_pct"],
        max_volatility_pct=risk_cfg["max_volatility_pct"],
        min_risk_reward=risk_cfg["min_risk_reward"],
        cooldown_minutes=risk_cfg["cooldown_minutes"],
        kill_switch_path=project_root / risk_cfg.get("kill_switch_file", "KILL_SWITCH"),
        symbol_whitelist=symbols,
        # Optional keys: only passed when present, so RiskEngine's own defaults (and its own
        # bounds validation) stay the single source of truth when omitted -- same pattern
        # src/bot.py's own RiskEngine construction uses. Found while writing this module's
        # use_rank_order tests: this harness previously silently dropped any risk_cfg override
        # of these two keys (always falling back to RiskEngine's class defaults of 5 / 120
        # regardless of what config/risk_cfg actually said) -- a real live-bot/harness parity
        # gap, fixed here. It happened not to affect any committed report/gate result so far:
        # config/config.yaml's values (5, 120) are numerically identical to RiskEngine's class
        # defaults, and no recorded parameter_variant candidate in scripts/backtest_summary.json
        # has ever swept either key (see docs/p1c_rank_signals_validation.md).
        **{
            key: risk_cfg[key]
            for key in ("max_new_entries_per_window", "new_entry_window_minutes")
            if key in risk_cfg
        },
    )

    cash = float(start_equity)
    state: dict[str, _SymState] = {s: _SymState() for s in symbols}
    trades: list[dict[str, Any]] = []
    equity = np.empty(T)
    exposure = np.empty(T)

    close = {s: enriched_global[s]["close"].to_numpy() for s in symbols}
    open_ = {s: enriched_global[s]["open"].to_numpy() for s in symbols}
    atr_arr = {s: enriched_global[s]["atr"].to_numpy() for s in symbols}

    def prices_at(t: int) -> dict[str, float]:
        out = {}
        for s in symbols:
            c = close[s][t]
            if not math.isnan(c):
                out[s] = float(c)
        return out

    def invested_value(prices: dict[str, float]) -> float:
        total = 0.0
        for s in symbols:
            st = state[s]
            if st.status == "OPEN" and st.qty > 0:
                total += st.qty * prices.get(s, st.entry_px)
        return total

    def record_trade(symbol: str, t: int, reason: str, raw_exit_px: float) -> None:
        nonlocal cash
        st = state[symbol]
        px = raw_exit_px * (1.0 - costs.slippage)
        notional = st.qty * px
        fee_x = notional * costs.fee
        cash += notional - fee_x
        net = (notional - fee_x) - (st.qty * st.entry_px + st.entry_fee)
        r = net / (st.qty * st.planned_risk_per_unit) if st.planned_risk_per_unit > 0 else float("nan")
        trades.append(
            dict(
                symbol=symbol,
                entry_time=idx[st.entry_idx],
                exit_time=idx[t],
                entry_px=st.entry_px,
                exit_px=px,
                qty=st.qty,
                net_pnl=net,
                fees=st.entry_fee + fee_x,
                R=r,
                bars_held=t - st.entry_idx,
                reason=reason,
                entry_rank=0.0,
                expo_at_entry=st.entry_expo,
            )
        )
        state[symbol] = _SymState()

    for t in range(T):
        prices_now = prices_at(t)

        # ---- 1) execute whatever was queued from the PREVIOUS bar's close-trigger, at THIS bar's open ----
        for s in symbols:
            o = open_[s][t]
            if math.isnan(o):
                continue
            st = state[s]
            if st.queued is None:
                continue
            action, payload = st.queued
            st.queued = None
            if action == "FILL":
                quote_amount = min(payload or 0.0, cash)  # see module docstring note 5 (same-bar cash race)
                price = o * (1.0 + costs.slippage)
                if quote_amount <= 0 or price <= 0:
                    state[s] = _SymState()
                    continue
                fee_e = quote_amount * costs.fee
                net_quote = quote_amount - fee_e
                if net_quote <= 0:
                    state[s] = _SymState()
                    continue
                eq_before = cash + invested_value(prices_now)
                qty = net_quote / price
                cash -= quote_amount
                plan = st.plan
                st.status = "OPEN"
                st.qty = qty
                st.entry_px = price
                st.entry_fee = fee_e
                st.entry_idx = t
                st.planned_risk_per_unit = max(1e-12, plan["entry_zone"][1] - plan["stop_loss"])
                st.entry_expo = (qty * price) / eq_before if eq_before > 0 else 0.0
            elif action == "DISCARD":
                state[s] = _SymState()
            elif action in ("STOP_LOSS", "TAKE_PROFIT"):
                record_trade(s, t, action.lower(), o)

        # ---- 2) bar-close equity/exposure bookkeeping (post step 1) ----
        prices_now = prices_at(t)
        inv = invested_value(prices_now)
        equity[t] = cash + inv
        exposure[t] = inv / equity[t] if equity[t] > 0 else 0.0

        # ---- 3) decide the NEXT trigger for every symbol, from THIS bar's close ----
        # Processing order only matters for symbols that are FLAT with a fresh signal this bar
        # (the ones that could newly compete for risk_engine's GLOBAL max_new_entries_per_window
        # slot) -- OPEN/PENDING monitor checks and FLAT-with-no-signal symbols never compete for
        # anything, so their relative order is left exactly as before (`sorted(symbols)`).
        if use_rank_order:
            signals_t: dict[str, Signal] = {}
            for s in symbols:
                if state[s].status != "FLAT":
                    continue
                sig = signals_by_symbol.get(s, {}).get(idx[t])
                if sig is not None:
                    signals_t[s] = sig
            if signals_t:
                liquidity_t = {
                    s: float(liquidity_arr[s][t])
                    for s in symbols
                    if not math.isnan(liquidity_arr[s][t])
                }
                ranked = signal_engine.rank_signals(signals_t, liquidity_t)
                order_t = ranked + [s for s in symbols if s not in signals_t]
            else:
                order_t = symbols
        else:
            order_t = symbols

        for s in order_t:
            c = close[s][t]
            if math.isnan(c):
                continue
            st = state[s]
            price = float(c)

            if st.status == "OPEN":
                action = check_open(_as_plan_record(s, "OPEN", st.plan), price)
                if action is not None:
                    st.queued = (action.action, None)
            elif st.status == "PENDING":
                action = check_pending(_as_plan_record(s, "PENDING", st.plan), price)
                if action is not None:
                    amt = st.plan.get("approved_quote_amount") if action.action == "FILL" else None
                    st.queued = (action.action, amt)
            else:  # FLAT -- look for a brand new setup, same branch src/bot.py takes
                sig = signals_by_symbol.get(s, {}).get(idx[t])
                if sig is None:
                    continue
                atr_val = float(atr_arr[s][t]) if not math.isnan(atr_arr[s][t]) else price * 0.01
                plan = trade_planner.build_plan(sig, s, price, atr_val, **planner_cfg)
                if plan is None:
                    continue
                other_value = invested_value(prices_now)  # s itself has no position yet
                atr_pct = atr_val / price if price else None
                decision = risk_engine.check_new_trade(
                    s,
                    "buy",
                    portfolio_equity=equity[t],
                    cash_available=cash,
                    entry_price=plan.entry_zone[1],
                    stop_loss=plan.stop_loss,
                    risk_reward=plan.risk_reward,
                    open_positions_value=other_value,
                    atr_pct=atr_pct,
                    now=idx[t],
                )
                if not decision.allowed:
                    continue
                plan_dict = dict(asdict(plan))
                plan_dict["approved_quote_amount"] = decision.max_quote_amount
                st.status = "PENDING"
                st.plan = plan_dict
                risk_engine.record_trade(s, now=idx[t])  # cooldown starts at planning time, matches src/bot.py
                immediate = check_pending(_as_plan_record(s, "PENDING", plan_dict), price)
                if immediate is not None:
                    amt = plan_dict["approved_quote_amount"] if immediate.action == "FILL" else None
                    st.queued = (immediate.action, amt)

    # ---- close_at_end: force-close anything still OPEN, at that symbol's own last available price ----
    for s in symbols:
        st = state[s]
        if st.status != "OPEN":
            continue
        valid = np.where(~np.isnan(close[s]))[0]
        if len(valid) == 0:
            continue
        last_t = int(valid[-1])
        record_trade(s, last_t, "end", float(close[s][last_t]))
    if T > 0:
        equity[T - 1] = cash
        exposure[T - 1] = 0.0

    cols = [
        "symbol", "entry_time", "exit_time", "entry_px", "exit_px", "qty", "net_pnl", "fees",
        "R", "bars_held", "reason", "entry_rank", "expo_at_entry",
    ]
    trades_df = pd.DataFrame(trades, columns=cols) if trades else pd.DataFrame(columns=cols)
    cfg = EngineConfig(start_equity=start_equity, periods_per_year=periods_per_year)
    return Result(trades_df, pd.Series(equity, index=idx), pd.Series(exposure, index=idx), cfg, costs)


# --------------------------------------------------------------------------- #
# Time-shift placebo (adapted from engine.placebo_baseline's pattern -- see run_live_bot_report.py)
# --------------------------------------------------------------------------- #
def _dense_signal_array(sig_map: dict[pd.Timestamp, Signal], idx: pd.DatetimeIndex) -> np.ndarray:
    pos = {ts: i for i, ts in enumerate(idx)}
    arr = np.full(len(idx), None, dtype=object)
    for ts, sig in sig_map.items():
        i = pos.get(ts)
        if i is not None:
            arr[i] = sig
    return arr


def _placebo_signal_at(original: Signal, enriched_g: pd.DataFrame, i: int) -> Signal | None:
    """Rebuilds a same-recipe Signal anchored to the LANDING bar's own price/ADX, rather than
    transplanting the original bar's price levels (which would be meaningless at a different,
    randomly-shifted point in time). primary_details is left without a "level" key so
    src/trade_planner.py's own fallback (`_primary_level`: no level -> use current price) centers
    the zone on the landing bar's close, exactly like a freshly-detected pattern would -- see the
    module docstring's point (3). Real ADX at the landing bar is carried over so the trend-stop
    bonus logic still behaves faithfully."""
    if i >= len(enriched_g):
        return None
    row = enriched_g.iloc[i]
    close_px = row.get("close")
    if close_px is None or (isinstance(close_px, float) and math.isnan(close_px)):
        return None  # landed before this symbol's listing / after its last date
    adx_val = row.get("adx")
    details: dict[str, Any] = {}
    if adx_val is not None and not (isinstance(adx_val, float) and math.isnan(adx_val)):
        details["adx"] = float(adx_val)
    return Signal(
        direction="bullish",
        primary_pattern=original.primary_pattern,
        engine_score=original.engine_score,
        supporting_patterns=list(original.supporting_patterns),
        primary_details=details,
        rationale={"placebo": True, "original_pattern": original.primary_pattern},
    )


def placebo_run(
    enriched_global: dict[str, pd.DataFrame],
    signals_by_symbol: dict[str, dict[pd.Timestamp, Signal]],
    costs: Costs,
    risk_cfg: dict[str, Any],
    planner_cfg: dict[str, Any],
    start_equity: float,
    periods_per_year: float,
    project_root: Path,
    n_sims: int = 100,
    seed: int = 0,
    use_rank_order: bool = False,
    timeframe: str = "1d",
) -> pd.DataFrame:
    """Time-shift placebo, adapted from engine.placebo_baseline's pattern: circularly shift WHEN
    each symbol's real signals fired by one random offset shared by every symbol (so clustering/
    frequency/cross-coin structure survives the shift), rebuild same-recipe plans anchored to
    whatever REAL price/ATR prevailed at each shifted landing bar (see _placebo_signal_at), and
    rerun the exact same simulate_portfolio(). If the real signals' TIMING carries information,
    the real run should beat most of these shifted copies.

    `use_rank_order` is forwarded to `simulate_portfolio` unchanged (default False, matching
    every existing caller) so a placebo comparison uses the SAME per-bar processing order as
    whatever real run it's being compared against. `timeframe` (Phase 2.0 generalization, default
    "1d" preserves every existing caller's behavior byte-for-byte) is forwarded the same way, for
    the same liquidity-window reason documented on simulate_portfolio."""
    idx = next(iter(enriched_global.values())).index
    T = len(idx)
    dense = {s: _dense_signal_array(sig_map, idx) for s, sig_map in signals_by_symbol.items()}
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n_sims):
        k = int(rng.integers(int(T * 0.10), int(T * 0.90)))
        shifted: dict[str, dict[pd.Timestamp, Signal]] = {}
        for s, arr in dense.items():
            rolled = np.roll(arr, k)
            enr = enriched_global[s]
            sym_map: dict[pd.Timestamp, Signal] = {}
            for i, sig in enumerate(rolled):
                if sig is None:
                    continue
                placebo_sig = _placebo_signal_at(sig, enr, i)
                if placebo_sig is not None:
                    sym_map[idx[i]] = placebo_sig
            shifted[s] = sym_map
        res = simulate_portfolio(
            enriched_global, shifted, costs, risk_cfg, planner_cfg, start_equity, periods_per_year, project_root,
            use_rank_order=use_rank_order, timeframe=timeframe,
        )
        from engine import summarize  # local import: avoids a module-level cycle with run_live_bot_report

        m = summarize(res)
        rows.append(dict(total_return=m["total_return"], avg_R=m.get("avg_R", np.nan), trades=m["trades"]))
    return pd.DataFrame(rows)
