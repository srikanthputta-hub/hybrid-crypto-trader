"""`notifier.label` and the 1d "Valid until" lines (docs/multiday_hold_design.md §3.1/§6).

Groups:
  0. Startup validation of `notifier.label` (src.config.parse_notifier_settings, build_components).
  1. Default-config byte identity: with the key absent / null (and bar_parity off, or on at 1h) every
     message is exactly what a frozen copy of origin/main's `_maybe_notify` produces.
  2. The label prefixes BUY / SELL (stop-loss, take-profit) / fill messages and nothing else.
  3. At 1d with loop.bar_parity on, BUY messages end with "Bar:" / "Valid until:" lines
     (bar close + max_entry_delay_minutes, else the next daily close); nowhere else.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src import notifier
from src.bot import _maybe_notify, build_components
from src.config import NOTIFIER_LABEL_MAX_LEN, Config, NotifierSettings, load_config, parse_notifier_settings
from tests._pre_label_maybe_notify import pre_label_maybe_notify
from tests.test_bot_universe_wiring import _small_raw

BUY = {
    "symbol": "BTC/USD",
    "action": "planned",
    "bar_close": "2026-10-13T00:00:00+00:00",
    "plan": {
        "pattern": "BREAKOUT",
        "entry_zone": (100.0, 101.5),
        "stop_loss": 95.0,
        "take_profit": 112.0,
        "risk_reward": 2.0,
        "rule_based_confidence_stars": 4,
    },
}
STOP = {"symbol": "BTC/USD", "monitor_action": {"action": "STOP_LOSS"}, "execution": {"price": 95.0}}
TARGET = {"symbol": "BTC/USD", "monitor_action": {"action": "TAKE_PROFIT"}, "execution": {"price": 112.0}}
FILL = {"symbol": "BTC/USD", "monitor_action": {"action": "FILL"}, "execution": {"price": 101.0, "base_amount": 0.5}}
IMMEDIATE_FILL = {**BUY, "execution": {"price": 101.0, "base_amount": 0.5}}
NOTHING = {"symbol": "BTC/USD", "action": "none"}
EVENTS = [BUY, STOP, TARGET, FILL, IMMEDIATE_FILL, NOTHING]


def _real_cfg(monkeypatch, *, label="absent", parity=None, timeframe="1d", delay=None, telegram=True):
    """A real `Config` (so the real properties run) with Telegram env vars set."""
    raw = _small_raw(["BTC/USD"])
    raw["market"]["timeframe"] = timeframe
    raw["market"]["history_bars"] = 200
    if parity is not None:
        raw["loop"] = {"bar_parity": parity}
        if delay is not None:
            raw["loop"]["max_entry_delay_minutes"] = delay
    if label != "absent":
        raw["notifier"] = {"label": label}
    if telegram:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    else:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    return Config(raw=raw)


def _messages(fn, cfg, event) -> list[str]:
    with patch("src.notifier.send_telegram_message") as send:
        fn(cfg, event)
    return [call.args[2] for call in send.call_args_list]


# --- 0. startup validation ----------------------------------------------------


def test_absent_and_null_mean_no_label():
    assert parse_notifier_settings({}) == NotifierSettings(label=None)
    assert parse_notifier_settings({"notifier": None}) == NotifierSettings(label=None)
    assert parse_notifier_settings({"notifier": {}}).label is None
    assert parse_notifier_settings({"notifier": {"label": None}}).label is None


def test_real_config_has_no_label(monkeypatch):
    # Merging must not change the running bot's messages: config.yaml spells the key out as null.
    cfg = load_config()
    assert "notifier" in cfg.raw and cfg.raw["notifier"] == {"label": None}
    assert cfg.notifier_label is None


@pytest.mark.parametrize("good", ["PAPER TRIAL", "A", "paper-trial 1d", "x" * NOTIFIER_LABEL_MAX_LEN, "PAPER (1d)", "🧪 PAPER"])
def test_valid_labels_are_accepted(good):
    assert parse_notifier_settings({"notifier": {"label": good}}).label == good


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        " PAPER",
        "PAPER ",
        "x" * (NOTIFIER_LABEL_MAX_LEN + 1),
        "PAPER\nTRIAL",
        "PAPER\tTRIAL",
        "PAPER\rTRIAL",
        "\x00",
        "PAPER\x1b[31m",
        "PAPER\x7f",
        "PAPER TRIAL",
        "PAPER​TRIAL",
        "PAPER‮TRIAL",
        "PAPER_TRIAL",
        "*PAPER*",
        "PAPER`TRIAL",
        "[PAPER]",
        "PAPER\\TRIAL",
        5,
        1.5,
        True,
        False,
        ["PAPER TRIAL"],
        {"a": 1},
    ],
)
def test_bad_labels_are_refused(bad):
    with pytest.raises(ValueError, match="notifier.label"):
        parse_notifier_settings({"notifier": {"label": bad}})


def test_unknown_notifier_keys_and_non_mapping_sections_are_refused():
    with pytest.raises(ValueError, match="lable"):
        parse_notifier_settings({"notifier": {"lable": "PAPER TRIAL"}})
    with pytest.raises(ValueError, match="mapping"):
        parse_notifier_settings({"notifier": "PAPER TRIAL"})
    with pytest.raises(ValueError, match="mapping"):
        parse_notifier_settings({"notifier": ["PAPER TRIAL"]})


def test_build_components_refuses_a_bad_label_at_startup(tmp_path):
    raw = _small_raw(["ETH/USDT"])
    raw["notifier"] = {"label": "PAPER_TRIAL"}
    with pytest.raises(ValueError, match="notifier.label"):
        build_components(Config(raw=raw))


def test_label_never_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("NOTIFIER_LABEL", "FROM ENV")
    assert _real_cfg(monkeypatch).notifier_label is None


# --- 1. default-config byte identity ------------------------------------------


@pytest.mark.parametrize("event", EVENTS, ids=["buy", "stop", "target", "fill", "immediate", "none"])
@pytest.mark.parametrize(
    "kw",
    [
        dict(),  # key absent, no loop section
        dict(label=None),
        dict(parity=False),
        dict(parity=False, label=None),
        dict(parity=True, timeframe="1h"),  # bar parity at 1h: no validity lines
        dict(parity=False, timeframe="1d"),  # 1d without bar parity: no validity lines
    ],
    ids=["absent", "null", "parity-off", "parity-off-null", "parity-on-1h", "1d-parity-off"],
)
def test_default_config_messages_are_byte_identical_to_main(monkeypatch, kw, event):
    cfg = _real_cfg(monkeypatch, **kw)
    new = _messages(_maybe_notify, cfg, copy.deepcopy(event))
    old = _messages(pre_label_maybe_notify, cfg, copy.deepcopy(event))
    assert new == old
    assert (len(new) == 0) == (event is NOTHING)


@pytest.mark.parametrize("event", EVENTS)
def test_simple_namespace_config_still_works_and_is_identical(event):
    # tests/test_bot.py configures _maybe_notify with a bare SimpleNamespace: no notifier_label,
    # no bar_parity, no timeframe attributes at all.
    cfg = SimpleNamespace(telegram_bot_token="token", telegram_chat_id="12345")
    assert _messages(_maybe_notify, cfg, copy.deepcopy(event)) == _messages(pre_label_maybe_notify, cfg, copy.deepcopy(event))


def test_default_buy_message_text_is_pinned(monkeypatch):
    assert _messages(_maybe_notify, _real_cfg(monkeypatch), BUY) == [
        "🔔 *BUY signal*: BTC/USD (BREAKOUT)\n"
        "Buy between $100.00 and $101.50\n"
        "Stop-loss: $95.00 (sell here to cap the loss)\n"
        "Target: $112.00 (take profit here)\n"
        "Risk:Reward 1:2.0  Confidence: 4/5"
    ]


def test_telegram_not_configured_sends_nothing_even_with_a_label(monkeypatch):
    cfg = _real_cfg(monkeypatch, label="PAPER TRIAL", telegram=False)
    assert _messages(_maybe_notify, cfg, BUY) == []


# --- 2. the label --------------------------------------------------------------


def test_label_prefixes_buy_sell_and_fill_messages(monkeypatch):
    cfg = _real_cfg(monkeypatch, label="PAPER TRIAL")
    base = _real_cfg(monkeypatch)
    for event in (BUY, STOP, TARGET, FILL, IMMEDIATE_FILL):
        labelled = _messages(_maybe_notify, cfg, copy.deepcopy(event))
        plain = _messages(_maybe_notify, base, copy.deepcopy(event))
        assert len(labelled) == len(plain) >= 1
        for l, p in zip(labelled, plain):
            assert l == "*PAPER TRIAL* · " + p  # only a prefix is added, nothing else changes
    assert _messages(_maybe_notify, cfg, NOTHING) == []


def test_label_messages_start_with_the_label(monkeypatch):
    cfg = _real_cfg(monkeypatch, label="PAPER TRIAL")
    assert _messages(_maybe_notify, cfg, BUY)[0].startswith("*PAPER TRIAL* · 🔔 *BUY signal*: BTC/USD")
    assert _messages(_maybe_notify, cfg, STOP)[0].startswith("*PAPER TRIAL* · 🏁 *SELL*")
    assert _messages(_maybe_notify, cfg, FILL)[0].startswith("*PAPER TRIAL* · ✅ *Bought*")


def test_label_with_a_simple_namespace_config():
    cfg = SimpleNamespace(telegram_bot_token="t", telegram_chat_id="c", notifier_label="PAPER TRIAL")
    assert _messages(_maybe_notify, cfg, STOP)[0].startswith("*PAPER TRIAL* · 🏁 *SELL*")


def test_label_prefix_helper():
    assert notifier.label_prefix(None) == ""
    assert notifier.label_prefix("") == ""
    assert notifier.label_prefix("PAPER TRIAL") == "*PAPER TRIAL* · "


# --- 3. bar / valid-until lines -------------------------------------------------


def test_buy_message_at_1d_with_bar_parity_has_bar_and_valid_until_with_delay(monkeypatch):
    cfg = _real_cfg(monkeypatch, parity=True, delay=60)
    [msg] = _messages(_maybe_notify, cfg, BUY)
    default = _messages(_maybe_notify, _real_cfg(monkeypatch), BUY)[0]
    assert msg == default + "\nBar: 2026-10-12 (daily, closed 2026-10-13 00:00 UTC)\nValid until: 2026-10-13 01:00 UTC"


def test_valid_until_is_the_next_daily_close_without_a_delay_guard(monkeypatch):
    cfg = _real_cfg(monkeypatch, parity=True)
    [msg] = _messages(_maybe_notify, cfg, BUY)
    assert msg.endswith("\nValid until: 2026-10-14 00:00 UTC")


def test_valid_until_handles_fractional_and_long_delays(monkeypatch):
    assert _messages(_maybe_notify, _real_cfg(monkeypatch, parity=True, delay=90.5), BUY)[0].endswith(
        "Valid until: 2026-10-13 01:30 UTC"
    )
    assert _messages(_maybe_notify, _real_cfg(monkeypatch, parity=True, delay=1500), BUY)[0].endswith(
        "Valid until: 2026-10-14 01:00 UTC"
    )


def test_label_and_validity_combine(monkeypatch):
    cfg = _real_cfg(monkeypatch, label="PAPER TRIAL", parity=True, delay=60)
    [msg] = _messages(_maybe_notify, cfg, BUY)
    assert msg.startswith("*PAPER TRIAL* · 🔔 *BUY signal*")
    assert msg.endswith("Valid until: 2026-10-13 01:00 UTC")


def test_validity_lines_only_on_buy_signal_messages(monkeypatch):
    cfg = _real_cfg(monkeypatch, label="PAPER TRIAL", parity=True, delay=60)
    for event in (STOP, TARGET, FILL):
        [msg] = _messages(_maybe_notify, cfg, event)
        assert "Valid until" not in msg and "Bar:" not in msg
    # the immediate-fill event sends the BUY signal and then the fill; only the first has the lines
    signal, fill = _messages(_maybe_notify, cfg, IMMEDIATE_FILL)
    assert "Valid until" in signal and "Valid until" not in fill


def test_no_validity_lines_at_1h_or_without_bar_parity(monkeypatch):
    for kw in (dict(parity=True, timeframe="1h"), dict(parity=False), dict()):
        [msg] = _messages(_maybe_notify, _real_cfg(monkeypatch, **kw), BUY)
        assert "Valid until" not in msg


@pytest.mark.parametrize("bar_close", [None, "", "not a date", 12345])
def test_missing_or_garbage_bar_close_adds_nothing(monkeypatch, bar_close):
    event = {**BUY, "bar_close": bar_close}
    cfg = _real_cfg(monkeypatch, parity=True, delay=60)
    assert _messages(_maybe_notify, cfg, event) == _messages(_maybe_notify, _real_cfg(monkeypatch), event)
    event.pop("bar_close")
    assert _messages(_maybe_notify, cfg, event) == _messages(_maybe_notify, _real_cfg(monkeypatch), event)


def test_bar_validity_text_treats_naive_times_as_utc_and_converts_offsets():
    assert notifier.bar_validity_text("2026-10-13T00:00:00", 60).endswith("Valid until: 2026-10-13 01:00 UTC")
    # 02:00+02:00 is 00:00 UTC
    assert notifier.bar_validity_text("2026-10-13T02:00:00+02:00", 60) == notifier.bar_validity_text(
        "2026-10-13T00:00:00+00:00", 60
    )


def test_notifier_plain_text_is_markdown_safe():
    text = notifier.bar_validity_text("2026-10-13T00:00:00+00:00", 60)
    assert not set("*_`[") & set(text)
    assert not set("*_`[]\\") & set(notifier.label_prefix("PAPER TRIAL").replace("*PAPER TRIAL*", ""))


def test_label_is_not_escaped_but_dynamic_fields_are(monkeypatch):
    """PR #103: symbol / pattern go through escape_md; the label sits inside its own *...* entity, is validated at
    startup (no Markdown specials) and must stay verbatim."""
    event = {**BUY, "symbol": "BTC_X/USD", "plan": {**BUY["plan"], "pattern": "TREND_CONTINUATION"}}
    [msg] = _messages(_maybe_notify, _real_cfg(monkeypatch, label="PAPER TRIAL"), event)
    assert msg.startswith("*PAPER TRIAL* · 🔔 *BUY signal*: BTC\\_X/USD (TREND\\_CONTINUATION)\n")
    [plain] = _messages(_maybe_notify, _real_cfg(monkeypatch), event)
    assert msg == "*PAPER TRIAL* · " + plain
