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
possible, even during a kill switch or a daily-loss halt.

Plus a GLOBAL new-entry rate cap: no more than max_new_entries_per_window new
entries (across ALL symbols combined) may be approved within any rolling
new_entry_window_minutes. cooldown_minutes only stops the *same* symbol from
re-entering; this stops a burst of *different* symbols all triggering on the
same market-wide move (one shared directional bet, not N independent edges)
from being approved together. It only ever rejects a new entry — it never
resizes one and never touches an open position.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


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
    ) -> None:
        # A floor above 1.0 would *enlarge* positions in volatile markets, and a
        # negative/non-finite one is meaningless — refuse to start rather than guess.
        if not (math.isfinite(volatility_scale_floor) and 0.0 <= volatility_scale_floor <= 1.0):
            raise ValueError(
                f"volatility_scale_floor must be a finite number in [0, 1], got {volatility_scale_floor!r}"
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
        if (
            isinstance(new_entry_window_minutes, bool)
            or not isinstance(new_entry_window_minutes, (int, float))
            or not math.isfinite(new_entry_window_minutes)
            or new_entry_window_minutes <= 0
        ):
            raise ValueError(
                f"new_entry_window_minutes must be a finite number > 0, got {new_entry_window_minutes!r}"
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

    def _refresh_daily_baseline(self, current_equity: float, now: datetime) -> None:
        today = now.date()
        if self._day_start_date != today:
            self._day_start_date = today
            self._day_start_equity = current_equity

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
            # Closing an existing position reduces risk — never block it.
            return RiskDecision(True, "ok")

        if self.kill_switch_path.exists():
            return RiskDecision(False, f"kill switch file present at {self.kill_switch_path}")

        if symbol not in self.symbol_whitelist:
            return RiskDecision(False, f"{symbol} is not in the configured symbol whitelist")

        assert self._day_start_equity is not None
        daily_pnl_pct = (portfolio_equity - self._day_start_equity) / self._day_start_equity
        if daily_pnl_pct <= -abs(self.max_daily_loss_pct):
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

        if atr_pct is not None and atr_pct > self.max_volatility_pct:
            return RiskDecision(
                False,
                f"market volatility too high ({atr_pct:.2%} > max {self.max_volatility_pct:.2%}); "
                "skipping new entries until it calms down",
            )

        if risk_reward is not None and risk_reward < self.min_risk_reward:
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
                spend = min(spend, risk_sized_spend)

        remaining_exposure_room = max(0.0, portfolio_equity * self.max_total_exposure_pct - open_positions_value)
        spend = min(spend, remaining_exposure_room)

        if spend <= 0:
            return RiskDecision(
                False,
                "no room left to size this trade (cash, position cap, risk-per-trade cap, or "
                "total-exposure cap all reduced it to zero)",
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
