"""Frozen copy of `origin/main`'s `src.bot._maybe_notify` (at af99b64), before `notifier.label` and the
1d "Valid until" lines existed. The "before" half of the default-config byte-identity tests in
tests/test_notifier_label.py -- do not edit it to follow src/bot.py."""
from __future__ import annotations

from src import notifier
from src.bot import _fmt_price
from src.config import Config


def pre_label_maybe_notify(cfg: Config, event: dict) -> None:
    """Sends a Telegram message for the events worth interrupting someone's day for:
    a new signal, a fill, or a close. Plain-language/USD-formatted, since these are
    read by a person deciding whether to place the same trade on their own exchange,
    not just logged for a machine. Everything else (no-op cycles, discards, vetoes)
    stays in the audit log only. No-ops entirely if Telegram isn't configured (see
    src/notifier.py)."""
    bot_token, chat_id = cfg.telegram_bot_token, cfg.telegram_chat_id
    if not bot_token or not chat_id:
        return

    if event.get("action") == "planned" and "plan" in event:
        plan = event["plan"]
        entry_low, entry_high = plan.get("entry_zone", (None, None))
        notifier.send_telegram_message(
            bot_token,
            chat_id,
            f"🔔 *BUY signal*: {event['symbol']} ({plan.get('pattern')})\n"
            f"Buy between {_fmt_price(entry_low)} and {_fmt_price(entry_high)}\n"
            f"Stop-loss: {_fmt_price(plan.get('stop_loss'))} (sell here to cap the loss)\n"
            f"Target: {_fmt_price(plan.get('take_profit'))} (take profit here)\n"
            f"Risk:Reward 1:{plan.get('risk_reward')}  Confidence: {plan.get('rule_based_confidence_stars')}/5",
        )

    execution = event.get("execution")
    monitor_action = (event.get("monitor_action") or {}).get("action")
    if execution and monitor_action in ("STOP_LOSS", "TAKE_PROFIT"):
        reason = "Stop-loss hit" if monitor_action == "STOP_LOSS" else "Target reached"
        notifier.send_telegram_message(
            bot_token,
            chat_id,
            f"🏁 *SELL* — trade closed: {event['symbol']}\n"
            f"Reason: {reason}\n"
            f"Price: {_fmt_price(execution.get('price'))}",
        )
    elif execution:
        # A monitor-triggered FILL, or the immediate-fill-on-plan path (monitor_action is
        # unset there since it bypasses monitor.check_pending's normal call site).
        notifier.send_telegram_message(
            bot_token,
            chat_id,
            f"✅ *Bought*: {event['symbol']}\n"
            f"Price: {_fmt_price(execution.get('price'))}  Amount: {execution.get('base_amount')}",
        )
