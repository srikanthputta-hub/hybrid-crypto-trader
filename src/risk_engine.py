"""Risk engine: the last line of defense before any order is placed.

This module knows nothing about strategies, patterns, or Claude — it only
answers one question, "given the current portfolio state and this specific
proposed trade plan, is it allowed right now, and if so how big?" Every check
here fails *closed* (on any doubt, the answer is "no trade" or "smaller
trade"), and it never widens a trade plan's own stop/target — only whether
and how much to buy.

Checks, matching the risk-engine design this bot follows:
  1. POSITION SIZE     — sized from the trade's own risk (equity fraction risked
                          divided by stop distance), then capped by the flat
                          max_position_pct ceiling.
  2. TOTAL EXPOSURE     — checks total capital already committed across every
                          open position, and won't let a new one push it over
                          max_total_exposure_pct of equity.
  3. RISK / REWARD      — rejects a plan whose risk/reward ratio is below
                          min_risk_reward, regardless of how confident anything
                          upstream was.
  4. MAX LOSS           — position size is capped so a stop-out never loses
                          more than max_loss_per_trade_pct of total equity.
  5. MARKET VOLATILITY  — rejects new entries when ATR as a fraction of price
                          exceeds max_volatility_pct (too chaotic to size risk
                          sensibly right now). Below that hard cutoff, the
                          per-trade risk budget is also tapered down as ATR
                          climbs from half of max_volatility_pct up to the
                          cap (to volatility_scale_floor at the cap) — this
                          can only ever shrink a position, never grow one.

Plus the account-level guards from before: symbol whitelist, daily loss limit,
per-symbol cooldown, and a kill switch file. Selling (closing an existing
position) is never blocked by any of this — closing risk should always be
possible, even during a kill switch or a daily-loss halt. The whitelist starts
as the constructor's symbol_whitelist and can be replaced at runtime with
set_symbol_whitelist() (e.g. on a universe refresh); either way it only gates
NEW buys.

Plus a GLOBAL new-entry rate cap: no more than max_new_entries_per_window new
entries (across ALL symbols combined) may be approved within any rolling
new_entry_window_minutes. cooldown_minutes only stops the *same* symbol from
re-entering; this stops a burst of *different* symbols all triggering on the
same market-wide move (one shared directional bet, not N independent edges)
from being approved together. It only ever rejects a new entry — it never
resizes one and never touches an open position.

Plus a MINIMUM ORDER SIZE floor (min_order_quote_usd, issue #81): if the final
spend, after every sizing cap above, is below this many quote-currency units,
the buy is rejected outright — a "dust" position that may also fall below the
exchange's own minimum order size. It never sizes a trade up to the floor.
0 (the default) disables it.

Plus NUMERIC INPUT VALIDATION for new buys (issue #87): every comparison above
is written as "reject if x <= limit" / "reject if x > limit", and with a NaN
operand each of those is False -- i.e. a NaN silently *passes* the check, and
min(x, nan) silently drops a cap. So before any limit is evaluated, a buy is
rejected outright (reason prefixed "invalid risk input:") unless:
  - portfolio_equity is a finite number > 0,
  - cash_available is a finite number (zero/negative still means "no room"),
  - open_positions_value is a finite number >= 0,
  - entry_price, if given, is a finite number > 0,
  - stop_loss and risk_reward, if given, are finite numbers,
  - atr_pct, if given, is a finite number >= 0.
bool / numpy.bool_ (and anything that isn't a numbers.Real, e.g. a numpy array) is
never accepted as a number. None keeps its existing meaning ("not
provided") for the optional inputs.

The same rule applies to the CONFIG: the constructor refuses to build an engine
(ValueError naming the key) unless every numeric risk setting is a finite int/float
in its documented range -- see __init__. A NaN limit would otherwise silently turn
its check off on every call (e.g. a NaN max_daily_loss_pct approved a buy at -20% on
the day). The daily-loss baseline is only ever
initialised from a finite equity > 0, so one bad equity reading can't become
the day's reference point. Sells are deliberately NOT validated: closing risk
must stay possible on a bad price/equity reading (see check_new_trade).
"""
from __future__ import annotations

import math
import numbers
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:  # numpy is a project dependency, but this module must not hard-require it.
    import numpy as _np

    _BOOL_TYPES: tuple[type, ...] = (bool, _np.bool_)
except ImportError:  # pragma: no cover - numpy is in requirements.txt
    _BOOL_TYPES = (bool,)


def _is_finite_number(value: object) -> bool:
    """True only for a real, finite number. bool and numpy.bool_ are rejected (math.isfinite
    accepts both as 0/1); so is anything that isn't a numbers.Real (e.g. a 0-d or 1-element
    numpy array, which math.isfinite would otherwise accept, or a Decimal). Anything
    math.isfinite can't evaluate (None, str, an int too large for a float, ...) counts as
    not finite -- fail closed, never "assume it's fine"."""
    if isinstance(value, _BOOL_TYPES) or not isinstance(value, numbers.Real):
        return False
    try:
        return math.isfinite(value)
    except (TypeError, ValueError, OverflowError):
        return False


def _require_config_number(name: str, value: object, expected: str, in_range: Callable[[float], bool]) -> None:
    """Startup validation of one numeric risk setting: raise ValueError naming `name` unless
    `value` is an int or float (bool / numpy.bool_ rejected -- True would silently mean 1),
    finite, and `in_range(value)`. Fails at construction rather than on every buy: e.g. a
    NaN max_daily_loss_pct would otherwise make `daily_pnl_pct <= -nan` False on every
    call, silently switching the daily loss limit off."""
    ok = False
    if not isinstance(value, _BOOL_TYPES) and isinstance(value, (int, float)):
        try:
            ok = math.isfinite(value) and bool(in_range(value))
        except OverflowError:  # an int too large to convert to float
            ok = False
    if not ok:
        raise ValueError(f"{name} must be {expected}, got {value!r}")


def _in_unit_interval_excl_zero(x: float) -> bool:
    return 0 < x <= 1


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    max_quote_amount: float | None = None  # how much (in quote currency, e.g. USDT) may be spent


class RiskEngine:
    def __init__(
        self,
        starting_cash_usd: float,
        max_position_pct: float,
        max_total_exposure_pct: float,
        max_loss_per_trade_pct: float,
        max_daily_loss_pct: float,
        max_volatility_pct: float,
        min_risk_reward: float,
        cooldown_minutes: int,
        kill_switch_path: Path,
        symbol_whitelist: list[str],
        stop_loss_pct: float = 0.03,
        take_profit_pct: float = 0.06,
        volatility_scale_floor: float = 0.5,
        max_new_entries_per_window: int = 5,
        new_entry_window_minutes: float = 120,
        min_order_quote_usd: float = 0.0,
    ) -> None:
        # Every numeric risk setting is validated here, at startup, so a bad config
        # stops the process instead of silently disabling a check on every buy: each
        # limit below is a comparison that a NaN passes (`nan <= x` is False), so e.g.
        # a NaN max_daily_loss_pct used to approve a buy at -20% on the day, and a NaN
        # cooldown_minutes raised on every call instead of once at startup. Ranges are
        # the units each value is actually used in by check_new_trade (all *_pct are
        # fractions, 0.05 == 5%), and match scripts/validate_invariants.py's CI check
        # of config/config.yaml. int or float only; bool / numpy.bool_ rejected.
        _require_config_number("starting_cash_usd", starting_cash_usd, "a finite number > 0", lambda x: x > 0)
        # Fractions of equity; > 1 would allow leverage this spot bot doesn't have, 0 would
        # silently turn the cap into "never trade" (or, for the daily limit, halt at -0%).
        for key, value in (
            ("max_position_pct", max_position_pct),
            ("max_total_exposure_pct", max_total_exposure_pct),
            ("max_loss_per_trade_pct", max_loss_per_trade_pct),
            ("max_daily_loss_pct", max_daily_loss_pct),
        ):
            _require_config_number(key, value, "a finite fraction of equity in (0, 1]", _in_unit_interval_excl_zero)
        # ATR / price, as a fraction (0.08 == 8%). 0 would reject every entry with ATR > 0.
        _require_config_number(
            "max_volatility_pct", max_volatility_pct, "a finite ATR/price fraction in (0, 1]", _in_unit_interval_excl_zero
        )
        # reward / risk ratio. Below 1.0 would accept plans that risk more than they target.
        _require_config_number("min_risk_reward", min_risk_reward, "a finite number >= 1.0", lambda x: x >= 1.0)
        # Minutes; 0 disables the per-symbol cooldown. Must also be representable as a
        # timedelta (check_new_trade builds one per call), so an absurd value fails here.
        _require_config_number("cooldown_minutes", cooldown_minutes, "a finite number of minutes >= 0", lambda x: x >= 0)
        try:
            timedelta(minutes=cooldown_minutes)
        except OverflowError as exc:
            raise ValueError(f"cooldown_minutes is too large: {cooldown_minutes!r}") from exc
        # Fallback fixed-exit fractions (stop_loss_price/take_profit_price). A stop of
        # >= 100% would sit at or below zero and never trigger.
        _require_config_number("stop_loss_pct", stop_loss_pct, "a finite number in (0, 1)", lambda x: 0 < x < 1)
        _require_config_number("take_profit_pct", take_profit_pct, "a finite number > 0", lambda x: x > 0)

        # Minimum order size floor (issue #81). 0 disables it (the pre-#81 behaviour).
        # Same "refuse to start rather than guess" rule as below: a negative or
        # non-finite floor is meaningless (inf would silently block every buy), and
        # bool is rejected explicitly because it is an int subclass.
        _require_config_number("min_order_quote_usd", min_order_quote_usd, "a finite number >= 0", lambda x: x >= 0)
        self.min_order_quote_usd = float(min_order_quote_usd)

        # A floor above 1.0 would *enlarge* positions in volatile markets, and a
        # negative/non-finite one is meaningless — refuse to start rather than guess.
        _require_config_number(
            "volatility_scale_floor", volatility_scale_floor, "a finite number in [0, 1]", lambda x: 0.0 <= x <= 1.0
        )
        self.volatility_scale_floor = volatility_scale_floor

        # Global new-entry rate cap. Same "refuse to start rather than guess" rule:
        # a count below 1 would block every entry forever (or, if negative/fractional,
        # make the comparison meaningless), and a zero/negative/non-finite window would
        # make the cap never count anything. bool is rejected explicitly because it is
        # an int subclass (True would silently mean "1").
        if (
            isinstance(max_new_entries_per_window, bool)
            or not isinstance(max_new_entries_per_window, int)
            or max_new_entries_per_window < 1
        ):
            raise ValueError(
                f"max_new_entries_per_window must be an integer >= 1, got {max_new_entries_per_window!r}"
            )
        _require_config_number(
            "new_entry_window_minutes", new_entry_window_minutes, "a finite number > 0", lambda x: x > 0
        )
        try:
            # Built once here so an absurdly large window fails at startup, not mid-cycle.
            self._new_entry_window = timedelta(minutes=new_entry_window_minutes)
        except OverflowError as exc:
            raise ValueError(f"new_entry_window_minutes is too large: {new_entry_window_minutes!r}") from exc
        if self._new_entry_window <= timedelta(0):
            # timedelta has microsecond resolution: a tiny positive value rounds to zero,
            # which would make the cap silently count nothing.
            raise ValueError(
                f"new_entry_window_minutes is too small to represent: {new_entry_window_minutes!r}"
            )
        self.max_new_entries_per_window = max_new_entries_per_window
        self.new_entry_window_minutes = new_entry_window_minutes
        self.starting_cash_usd = starting_cash_usd
        self.max_position_pct = max_position_pct
        self.max_total_exposure_pct = max_total_exposure_pct
        self.max_loss_per_trade_pct = max_loss_per_trade_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_volatility_pct = max_volatility_pct
        self.min_risk_reward = min_risk_reward
        self.cooldown_minutes = cooldown_minutes
        self.kill_switch_path = kill_switch_path
        self.symbol_whitelist = set(symbol_whitelist)
        # Fallback fixed exit thresholds, only used when a position has no
        # trade-plan-derived stop/target attached (e.g. legacy/manual positions).
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct

        self._last_trade_time: dict[str, datetime] = {}
        # Timestamps of the most recent new entries across ALL symbols, sorted
        # ascending and truncated to the newest max_new_entries_per_window. That is
        # all the cap needs: it is hit iff every one of the newest N entries is still
        # inside the window (any older entry is older still), so the list stays
        # bounded on a long-running host without any wall-clock-based pruning.
        self._recent_entry_times: list[datetime] = []
        self._day_start_equity: float | None = None
        self._day_start_date = None

    def set_symbol_whitelist(self, symbols: Iterable[str]) -> None:
        """Replace the symbol whitelist wholesale (e.g. on every universe refresh).

        Same meaning as the constructor's symbol_whitelist: the symbols this engine
        may approve NEW buys on. It never resizes a trade, never blocks a sell, and
        leaves per-symbol cooldowns and the global new-entry window untouched — a
        held position whose symbol drops out of the whitelist can still be closed.

        Replaces, never merges. An empty iterable is valid and blocks every new
        buy. Fails closed on nonsense rather than guessing: a bare str/bytes (which
        would otherwise become a whitelist of single characters), a non-iterable,
        or any non-string/blank entry raises, and the previous whitelist stays in
        effect unchanged (the new set is fully built before it is swapped in).
        """
        if isinstance(symbols, (str, bytes, bytearray)):
            raise TypeError(f"symbols must be an iterable of symbol strings, not a bare {type(symbols).__name__}")
        new_whitelist = frozenset(symbols)
        for symbol in new_whitelist:
            if not isinstance(symbol, str):
                raise TypeError(f"whitelist entries must be strings, got {symbol!r}")
            if not symbol.strip():
                raise ValueError(f"whitelist entries must be non-blank, got {symbol!r}")
        self.symbol_whitelist = new_whitelist

    def _refresh_daily_baseline(self, current_equity: float, now: datetime) -> None:
        # Issue #87: never adopt a non-finite / non-positive equity as the day's
        # baseline. A NaN baseline makes every later daily_pnl_pct NaN, and
        # `nan <= -limit` is False, so the daily loss limit would be silently off for
        # the rest of the UTC day; a zero baseline would raise ZeroDivisionError on
        # every buy. Skipping the refresh leaves the date unchanged, so the next call
        # with a valid equity initialises today's baseline; meanwhile any buy carrying
        # the invalid equity is rejected by check_new_trade's input validation.
        if not (_is_finite_number(current_equity) and current_equity > 0):
            return
        today = now.date()
        if self._day_start_date != today:
            self._day_start_date = today
            self._day_start_equity = current_equity

    @staticmethod
    def _invalid_buy_input(
        portfolio_equity: object,
        cash_available: object,
        entry_price: object,
        stop_loss: object,
        risk_reward: object,
        open_positions_value: object,
        atr_pct: object,
    ) -> str | None:
        """Reason string if any numeric input to a BUY is unusable, else None (issue #87).

        Every limit in check_new_trade is a comparison that a NaN silently passes
        (`nan <= x`, `nan < x`, `nan > x` are all False) and min()/max() silently drop a
        NaN operand depending on argument order, so these must be rejected up front
        rather than reasoned about check by check. Only *impossible* values are rejected
        here; a finite value that is merely unfavourable (zero/negative cash, a poor
        risk/reward, high ATR) still goes through the normal check that owns it, with
        its usual reason.
        """
        if not (_is_finite_number(portfolio_equity) and portfolio_equity > 0):  # type: ignore[operator]
            return f"invalid risk input: portfolio_equity={portfolio_equity!r} is not a finite number > 0"
        if not _is_finite_number(cash_available):
            return f"invalid risk input: cash_available={cash_available!r} is not a finite number"
        if not (_is_finite_number(open_positions_value) and open_positions_value >= 0):  # type: ignore[operator]
            return f"invalid risk input: open_positions_value={open_positions_value!r} is not a finite number >= 0"
        if entry_price is not None and not (_is_finite_number(entry_price) and entry_price > 0):  # type: ignore[operator]
            return f"invalid risk input: entry_price={entry_price!r} is not a finite number > 0"
        if stop_loss is not None and not _is_finite_number(stop_loss):
            return f"invalid risk input: stop_loss={stop_loss!r} is not a finite number"
        if risk_reward is not None and not _is_finite_number(risk_reward):
            return f"invalid risk input: risk_reward={risk_reward!r} is not a finite number"
        if atr_pct is not None and not (_is_finite_number(atr_pct) and atr_pct >= 0):  # type: ignore[operator]
            return f"invalid risk input: atr_pct={atr_pct!r} is not a finite number >= 0"
        return None

    def _volatility_scale(self, atr_pct: float | None) -> float:
        """Multiplier in [volatility_scale_floor, 1.0] applied to the per-trade risk budget.

        1.0 while ATR/price is at or below half of max_volatility_pct, then a linear
        taper down to volatility_scale_floor as ATR/price reaches max_volatility_pct
        (volatility-based sizing: shrink size as volatility rises). Never exceeds 1.0,
        so it can only ever shrink a position relative to unscaled sizing.
        """
        if atr_pct is None:
            return 1.0
        floor = self.volatility_scale_floor
        max_vol = self.max_volatility_pct
        # Fail closed on NaN/inf: a NaN scale would make risk_sized_spend NaN, and
        # min(spend, nan) silently returns spend — i.e. it would *drop* the per-trade
        # risk cap. Treat non-finite inputs as maximally uncertain instead.
        if not math.isfinite(atr_pct) or not math.isfinite(max_vol):
            return floor
        if max_vol <= 0:
            return 1.0
        lower = 0.5 * max_vol
        if atr_pct <= lower:
            return 1.0
        if atr_pct >= max_vol:
            return floor
        frac = (atr_pct - lower) / (max_vol - lower)
        scale = 1.0 - frac * (1.0 - floor)
        # Clamp against float rounding so the result can never leave [floor, 1.0].
        return min(1.0, max(floor, scale))

    def check_new_trade(
        self,
        symbol: str,
        side: str,
        portfolio_equity: float,
        cash_available: float,
        entry_price: float | None = None,
        stop_loss: float | None = None,
        risk_reward: float | None = None,
        open_positions_value: float = 0.0,
        atr_pct: float | None = None,
        now: datetime | None = None,
    ) -> RiskDecision:
        now = now or datetime.now(timezone.utc)
        self._refresh_daily_baseline(portfolio_equity, now)

        if side == "sell":
            # Closing an existing position reduces risk — never block it. This stays
            # true even when the numbers are garbage (issue #87): a NaN equity/price
            # is exactly the kind of moment an exit may be needed, and a sell sizes
            # nothing from these inputs (it closes the whole position), so there is no
            # NaN here that could make a sell *bigger*. _refresh_daily_baseline above
            # already refuses to adopt an invalid equity, so a sell can't poison it.
            return RiskDecision(True, "ok")

        if self.kill_switch_path.exists():
            return RiskDecision(False, f"kill switch file present at {self.kill_switch_path}")

        if symbol not in self.symbol_whitelist:
            return RiskDecision(False, f"{symbol} is not in the configured symbol whitelist")

        # Issue #87: reject non-finite / impossible numeric inputs BEFORE any limit is
        # evaluated -- every check below would otherwise silently pass a NaN.
        invalid = self._invalid_buy_input(
            portfolio_equity, cash_available, entry_price, stop_loss, risk_reward, open_positions_value, atr_pct
        )
        if invalid is not None:
            return RiskDecision(False, invalid)

        assert self._day_start_equity is not None
        daily_pnl_pct = (portfolio_equity - self._day_start_equity) / self._day_start_equity
        # The three limit comparisons below are written as `not (ok-condition)` so that a
        # NaN limit REJECTS (every comparison with NaN is False). The constructor already
        # refuses non-finite config; this only matters if an attribute is reassigned
        # after construction. Identical results for every finite value.
        if not (daily_pnl_pct > -abs(self.max_daily_loss_pct)):
            return RiskDecision(
                False,
                f"daily loss limit hit ({daily_pnl_pct:.2%} <= -{self.max_daily_loss_pct:.2%}); "
                "no new trades until tomorrow (UTC)",
            )

        last_trade = self._last_trade_time.get(symbol)
        if last_trade is not None:
            elapsed = now - last_trade
            if elapsed < timedelta(minutes=self.cooldown_minutes):
                remaining = timedelta(minutes=self.cooldown_minutes) - elapsed
                return RiskDecision(False, f"cooldown active for {symbol}: {remaining} remaining")

        # Global (all-symbol) new-entry rate cap. An entry counts while now - t < window
        # (same boundary convention as the cooldown above). A timestamp later than `now`
        # (clock step, out-of-order seed) yields a negative elapsed time and therefore
        # still counts — on doubt, the slot stays taken.
        in_window = [t for t in self._recent_entry_times if now - t < self._new_entry_window]
        if len(in_window) >= self.max_new_entries_per_window:
            next_slot_in = self._new_entry_window - (now - min(in_window))
            return RiskDecision(
                False,
                f"new-entry rate cap hit: {len(in_window)} new entries already approved across all "
                f"symbols in the last {self.new_entry_window_minutes:g} minutes "
                f"(max {self.max_new_entries_per_window}); next slot opens in {next_slot_in}",
            )

        if atr_pct is not None and not (atr_pct <= self.max_volatility_pct):
            return RiskDecision(
                False,
                f"market volatility too high ({atr_pct:.2%} > max {self.max_volatility_pct:.2%}); "
                "skipping new entries until it calms down",
            )

        if risk_reward is not None and not (risk_reward >= self.min_risk_reward):
            return RiskDecision(
                False, f"risk/reward {risk_reward:.2f} is below the configured minimum {self.min_risk_reward:.2f}"
            )

        # --- position sizing: risk-based first, then capped by flat/exposure/cash limits ---
        max_position_usd = portfolio_equity * self.max_position_pct
        spend = min(max_position_usd, cash_available)

        if entry_price is not None and stop_loss is not None and entry_price > stop_loss:
            stop_distance_pct = (entry_price - stop_loss) / entry_price
            if stop_distance_pct > 0:
                risk_budget_usd = portfolio_equity * self.max_loss_per_trade_pct * self._volatility_scale(atr_pct)
                risk_sized_spend = risk_budget_usd / stop_distance_pct
                if math.isnan(risk_sized_spend):
                    # Defence in depth (issue #87): unreachable with validated inputs and
                    # finite config, but min(spend, nan) returns spend -- it would silently
                    # DROP the per-trade risk cap (e.g. a NaN max_loss_per_trade_pct).
                    return RiskDecision(
                        False, f"invalid risk input: risk-sized spend is NaN ({risk_budget_usd!r} / {stop_distance_pct!r})"
                    )
                spend = min(spend, risk_sized_spend)

        remaining_exposure_room = max(0.0, portfolio_equity * self.max_total_exposure_pct - open_positions_value)
        spend = min(spend, remaining_exposure_room)

        if not math.isfinite(spend):
            # Defence in depth (issue #87): unreachable with validated inputs and finite
            # config (spend <= cash_available, which is finite), but a NaN spend would pass
            # `spend <= 0` below and be approved as max_quote_amount=nan.
            return RiskDecision(False, f"invalid risk input: sized spend {spend!r} is not a finite number")

        if spend <= 0:
            return RiskDecision(
                False,
                "no room left to size this trade (cash, position cap, risk-per-trade cap, or "
                "total-exposure cap all reduced it to zero)",
            )

        # Minimum order size (issue #81): applied LAST, to the final spend after every
        # cap above, and it only ever rejects -- it never sizes a trade up to the floor
        # (that would override the stricter caps). Written as `not (spend >= floor)` so
        # a NaN spend is rejected too whenever the floor is enabled.
        if self.min_order_quote_usd > 0 and not (spend >= self.min_order_quote_usd):
            return RiskDecision(
                False,
                f"sized order {spend:.8g} is below the minimum order size "
                f"{self.min_order_quote_usd:.8g} (min_order_quote_usd); rejected, not resized up",
            )
        return RiskDecision(True, "ok", max_quote_amount=spend)

    def record_trade(self, symbol: str, now: datetime | None = None, *, new_entry: bool = True) -> None:
        """Record that a trade plan for `symbol` was accepted (starts its cooldown).

        By default this also counts as one new entry toward the global
        max_new_entries_per_window cap. Pass new_entry=False only when re-recording a
        plan that was ALREADY counted when it was accepted (src/bot.py's deferred FILL
        path), so one plan isn't counted twice. The default is to count: a caller that
        forgets the flag over-counts (stricter), never under-counts.
        """
        ts = now or datetime.now(timezone.utc)
        self._last_trade_time[symbol] = ts
        if new_entry:
            self._add_recent_entry(ts)

    def seed_recent_entries(self, entry_times: Iterable[datetime | str | None], now: datetime | None = None) -> None:
        """Restore the global new-entry window after a process restart.

        The window lives in memory, so a restart (deploy, crash, systemd restart)
        would otherwise forget entries approved minutes earlier and re-open every
        slot. The caller passes the acceptance timestamps of plans it already knows
        about (e.g. each active plan's created_at). This only affects the global
        count, never per-symbol cooldowns. Fails closed: anything that isn't a
        parseable, timezone-aware timestamp counts as an entry made right `now`, and
        a timestamp in the future is clamped to `now` (so it holds a slot for exactly
        one window, rather than indefinitely).
        """
        now = now or datetime.now(timezone.utc)
        for raw in entry_times:
            ts: datetime | None = None
            if isinstance(raw, datetime):
                ts = raw
            elif isinstance(raw, str):
                try:
                    ts = datetime.fromisoformat(raw)
                except ValueError:
                    ts = None
            if ts is None or ts.tzinfo is None or ts.utcoffset() is None:
                ts = now
            elif ts > now:
                ts = now
            self._add_recent_entry(ts)

    def _add_recent_entry(self, ts: datetime) -> None:
        # Append first: if sorting ever raised (e.g. a naive/aware datetime mix), the
        # entry is still in the list and still counted — fail closed, not open.
        self._recent_entry_times.append(ts)
        self._recent_entry_times.sort()
        del self._recent_entry_times[: -self.max_new_entries_per_window]

    def stop_loss_price(self, entry_price: float) -> float:
        return entry_price * (1 - self.stop_loss_pct)

    def take_profit_price(self, entry_price: float) -> float:
        return entry_price * (1 + self.take_profit_pct)


# Backwards-compatible alias — earlier code/tests may still import RiskManager.
RiskManager = RiskEngine
