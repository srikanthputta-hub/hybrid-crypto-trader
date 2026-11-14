"""Issue #81 monitoring: scripts/check_pair_minimums.py (read-only, public data, no keys) and its
once-per-UTC-day, non-blocking wiring into scripts/run_live.py. Fake markets only: no network."""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import date
from types import SimpleNamespace

import ccxt
import pytest

from scripts import check_pair_minimums as cpm
from scripts import run_live

FLOOR = 10.0


def _market(amount_min=None, cost_min=0.5):
    return {"limits": {"amount": {"min": amount_min}, "cost": {"min": cost_min}}}


# --- evaluate(): the pure decision -----------------------------------------------------------------


def test_all_pairs_well_below_the_floor_is_ok():
    markets = {"BTC/USD": _market(0.00005), "SHIB/USD": _market(770000.0)}
    prices = {"BTC/USD": 90_000.0, "SHIB/USD": 0.0000068}          # $4.50 and $5.24
    rows, code = cpm.evaluate(["BTC/USD", "SHIB/USD"], markets, prices, FLOOR)
    assert code == cpm.EXIT_OK
    assert [r.status for r in rows] == ["OK", "OK"]
    assert rows[1].min_usd == pytest.approx(770000.0 * 0.0000068)


@pytest.mark.parametrize(
    ("price", "status", "code"),
    [
        (0.0000103, "OK", cpm.EXIT_OK),              # $7.93: below 80% of $10
        (0.0000104, "NEAR", cpm.EXIT_NEAR_OR_ABOVE),  # $8.01: within 20% of the floor
        (0.000013, "ABOVE", cpm.EXIT_NEAR_OR_ABOVE),  # $10.01
        (0.00002, "ABOVE", cpm.EXIT_NEAR_OR_ABOVE),   # $15.40: the SHIB minimum moved with its price
    ],
)
def test_base_unit_minimum_moves_with_price(price, status, code):
    rows, got = cpm.evaluate(["SHIB/USD"], {"SHIB/USD": _market(770000.0)}, {"SHIB/USD": price}, FLOOR)
    assert (rows[0].status, got) == (status, code)


def test_cost_minimum_counts_too():
    rows, code = cpm.evaluate(["BTC/USD"], {"BTC/USD": _market(None, 9.0)}, {"BTC/USD": 90_000.0}, FLOOR)
    assert rows[0].min_usd == 9.0 and rows[0].status == "NEAR" and code == cpm.EXIT_NEAR_OR_ABOVE


def test_margin_is_configurable():
    rows, code = cpm.evaluate(["X/USD"], {"X/USD": _market(1.0)}, {"X/USD": 8.5}, FLOOR, margin=0.1)
    assert rows[0].status == "OK" and code == cpm.EXIT_OK


@pytest.mark.parametrize(
    ("markets", "prices", "detail"),
    [
        ({}, {"BTC/USD": 1.0}, "not listed"),
        (None, {}, "could not be loaded"),
        ({"BTC/USD": {"limits": {"amount": {"min": float("nan")}}}}, {"BTC/USD": 1.0}, "finite"),
        ({"BTC/USD": {}}, {"BTC/USD": 1.0}, "limits"),
        ({"BTC/USD": _market(0.0001)}, {}, "no usable price"),
        ({"BTC/USD": _market(0.0001)}, {"BTC/USD": float("nan")}, "no usable price"),
        ({"BTC/USD": _market(0.0001)}, {"BTC/USD": True}, "no usable price"),
    ],
)
def test_anything_unknown_is_never_reported_fine(markets, prices, detail):
    rows, code = cpm.evaluate(["BTC/USD"], markets, prices, FLOOR)
    assert rows[0].status == "UNKNOWN" and detail in rows[0].detail and code == cpm.EXIT_UNKNOWN


def test_unknown_outranks_near():
    markets = {"A/USD": _market(1.0)}
    rows, code = cpm.evaluate(["A/USD", "B/USD"], markets, {"A/USD": 9.5}, FLOOR)
    assert [r.status for r in rows] == ["NEAR", "UNKNOWN"] and code == cpm.EXIT_UNKNOWN


def test_non_usd_quote_is_unknown():
    rows, code = cpm.evaluate(["ETH/BTC"], {"ETH/BTC": _market(0.01)}, {"ETH/BTC": 0.03}, FLOOR)
    assert rows[0].status == "UNKNOWN" and code == cpm.EXIT_UNKNOWN


def test_table_lists_every_pair_and_a_summary():
    rows, _ = cpm.evaluate(["BTC/USD", "SHIB/USD"], {"BTC/USD": _market(0.00005), "SHIB/USD": _market(770000.0)},
                           {"BTC/USD": 90_000.0, "SHIB/USD": 0.00002}, FLOOR)
    table = cpm.format_table(rows, FLOOR, 0.2)
    assert "BTC/USD" in table and "SHIB/USD" in table and "ABOVE" in table and "15.40" in table
    assert "summary: OK 1, NEAR 0, ABOVE 1, UNKNOWN 0" in table


# --- main(): fake public client, no keys ------------------------------------------------------------


class _PublicCcxt:
    def __init__(self, markets, tickers, error=None, has_tickers=True):
        self.markets, self.tickers, self.error = markets, tickers, error
        self.has = {"fetchTickers": has_tickers}
        self.calls = []

    def load_markets(self):
        self.calls.append("load_markets")
        if self.error is not None:
            raise self.error
        return self.markets

    def fetch_tickers(self, symbols):
        self.calls.append("fetch_tickers")
        return {s: {"last": self.tickers[s]} for s in symbols if s in self.tickers}

    def fetch_ticker(self, symbol):
        self.calls.append(f"fetch_ticker {symbol}")
        if symbol not in self.tickers:
            raise ccxt.BadSymbol(symbol)
        return {"last": self.tickers[symbol]}

    def __getattr__(self, name):  # anything else (orders, balances) must never be called
        raise AssertionError(f"check_pair_minimums called {name}")


def _config(tmp_path, symbols, floor=FLOOR):
    path = tmp_path / "config.yaml"
    sym = "\n".join(f'    - "{s}"' for s in symbols)
    path.write_text(f"mode: paper\nexchange:\n  id: kraken\n  testnet: false\nmarket:\n  symbols:\n{sym}\n"
                    f"risk:\n  min_order_quote_usd: {floor}\n")
    return path


def _client(ccxt_client):
    ex = cpm.ExchangeClient.__new__(cpm.ExchangeClient)
    ex.client, ex.exchange_id = ccxt_client, "kraken"
    return ex


@pytest.mark.parametrize("has_tickers", [True, False])
def test_main_ok_and_warn_exit_codes(tmp_path, capsys, has_tickers):
    cfg = _config(tmp_path, ["BTC/USD", "SHIB/USD"])
    markets = {"BTC/USD": _market(0.00005), "SHIB/USD": _market(770000.0)}
    fake = _PublicCcxt(markets, {"BTC/USD": 90_000.0, "SHIB/USD": 0.0000068}, has_tickers=has_tickers)
    assert cpm.main(["--config", str(cfg)], client=_client(fake)) == 0
    assert "RESULT: OK" in capsys.readouterr().out
    fake.tickers["SHIB/USD"] = 0.00002
    assert cpm.main(["--config", str(cfg)], client=_client(fake)) == 1
    out = capsys.readouterr().out
    assert "SHIB/USD" in out and "ABOVE" in out and "RESULT: WARN" in out


def test_main_markets_unavailable_is_unknown(tmp_path, capsys):
    cfg = _config(tmp_path, ["BTC/USD"])
    fake = _PublicCcxt({}, {}, error=ccxt.NetworkError("down"))
    assert cpm.main(["--config", str(cfg)], client=_client(fake)) == 2
    out = capsys.readouterr().out
    assert "load_markets failed (NetworkError)" in out and "RESULT: UNKNOWN" in out


def test_main_zero_floor_warns(tmp_path, capsys):
    cfg = _config(tmp_path, ["BTC/USD"], floor=0)
    fake = _PublicCcxt({"BTC/USD": _market(0.00005)}, {"BTC/USD": 90_000.0})
    assert cpm.main(["--config", str(cfg)], client=_client(fake)) == 1
    assert "no floor" in capsys.readouterr().out


def test_build_client_has_no_credentials_and_a_bounded_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("EXCHANGE_API_KEY", "should-never-be-used")
    monkeypatch.setenv("EXCHANGE_API_SECRET", "should-never-be-used")
    cfg = cpm.Config(raw=cpm._load_yaml(_config(tmp_path, ["BTC/USD"])))
    client = cpm.build_client(cfg, 7.5)                 # constructs ccxt.kraken: no network call
    assert not client.client.apiKey and not client.client.secret
    assert client.client.timeout == 7500


def test_script_never_loads_dotenv():
    source = open(cpm.__file__).read()
    assert "load_config" not in source and "load_dotenv" not in source and "api_key=None" in source


# --- scripts/run_live.py wiring: once per UTC day, separate process, never blocks / raises ------------


class _FakeProc:
    def __init__(self, code=0, output=b"", out=None):
        self.code, self.pid, self.killed = code, 4242, False
        self.running = True
        if out is not None:
            out.write(output)

    def poll(self):
        return None if self.running else self.code

    def kill(self):
        self.killed = True


class _Harness:
    def __init__(self, code=0, output=b"", start_error=None):
        self.started, self.sent, self.procs = [], [], []
        self.code, self.output, self.start_error = code, output, start_error
        self.now, self.day = 1000.0, date(2026, 11, 14)

    def popen(self, argv, **kw):
        if self.start_error is not None:
            raise self.start_error
        self.started.append((argv, kw))
        proc = _FakeProc(self.code, self.output, kw.get("stdout"))
        self.procs.append(proc)
        return proc

    def send(self, token, chat, text, timeout=10):
        self.sent.append((token, chat, text, timeout))
        return True

    def monitor(self):
        return run_live.PairMinimumsMonitor(popen=self.popen, clock=lambda: self.now, today=lambda: self.day,
                                            send=self.send)


CFG = SimpleNamespace(telegram_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", telegram_chat_id="42",
                      notifier_label="PAPER TRIAL")


def test_monitor_runs_at_most_once_per_utc_day_and_is_ok_silently(monkeypatch):
    monkeypatch.setenv("EXCHANGE_API_SECRET", "s3cret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", CFG.telegram_bot_token)
    h = _Harness(code=0)
    m = h.monitor()
    m.tick(CFG)
    assert len(h.started) == 1
    argv, kw = h.started[0]
    assert argv[1].endswith("check_pair_minimums.py") and "--timeout" in argv
    assert kw["start_new_session"] is True and kw["stdin"] is subprocess.DEVNULL
    assert "EXCHANGE_API_SECRET" not in kw["env"] and "TELEGRAM_BOT_TOKEN" not in kw["env"]
    m.tick(CFG)                                          # still running: just polled
    h.procs[0].running = False
    m.tick(CFG)                                          # finished OK: no Telegram
    for _ in range(5):
        m.tick(CFG)                                      # same UTC day: not started again
    assert len(h.started) == 1 and h.sent == [] and m.results == ["ok"]
    h.day = date(2026, 11, 15)
    m.tick(CFG)
    assert len(h.started) == 2


def test_monitor_warns_on_telegram_escaped_and_redacted_when_the_check_fails():
    output = (f"SHIB/USD ABOVE min_order_quote_usd *x* token {CFG.telegram_bot_token}\nRESULT: WARN").encode()
    h = _Harness(code=1, output=output)
    m = h.monitor()
    m.tick(CFG)
    h.procs[0].running = False
    m.tick(CFG)
    assert len(h.sent) == 1
    token, chat, text, timeout = h.sent[0]
    assert (token, chat) == (CFG.telegram_bot_token, "42") and timeout <= 10
    assert "exit code 1" in text and "SHIB/USD ABOVE" in text and "RESULT: WARN" in text
    assert CFG.telegram_bot_token not in text and "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in text
    assert r"min\_order\_quote\_usd" in text and r"\*x\*" in text and "*PAPER TRIAL*" in text


def test_monitor_kills_a_hung_check_and_warns(monkeypatch):
    killed_groups = []
    monkeypatch.setattr(run_live.os, "killpg", lambda pid, sig: killed_groups.append((pid, sig)))
    h = _Harness()
    m = h.monitor()
    m.tick(CFG)
    h.now += run_live.PAIR_MINIMUMS_TIMEOUT_SECONDS - 1
    m.tick(CFG)
    assert killed_groups == [] and h.sent == []          # inside the budget: left alone
    h.now += 2
    m.tick(CFG)
    assert killed_groups == [(4242, 9)]
    assert len(h.sent) == 1 and "did not finish" in h.sent[0][2] and m.proc is None


def test_monitor_start_failure_warns_and_never_raises():
    h = _Harness(start_error=OSError("no python"))
    m = h.monitor()
    m.tick(CFG)
    m.tick(CFG)
    assert len(h.sent) == 1 and "could not start (OSError)" in h.sent[0][2]


def test_monitor_internal_error_is_swallowed():
    h = _Harness()
    m = h.monitor()
    m._today = lambda: (_ for _ in ()).throw(RuntimeError("clock broke"))
    m.tick(CFG)                                          # no exception
    assert h.started == []


def test_monitor_with_a_real_subprocess_does_not_block():
    """A real (network-free) child process: the tick that starts it returns at once, a later
    tick collects its non-zero exit and output."""
    h = _Harness()
    script = "import time; time.sleep(0.5); print('SHIB/USD ABOVE'); raise SystemExit(1)"
    m = run_live.PairMinimumsMonitor(
        popen=lambda argv, **kw: subprocess.Popen([sys.executable, "-c", script], **kw),
        today=lambda: h.day, send=h.send)
    t0 = time.monotonic()
    m.tick(CFG)
    assert time.monotonic() - t0 < 0.4 and m.proc is not None
    deadline = time.monotonic() + 30
    while m.proc is not None and time.monotonic() < deadline:
        m.tick(CFG)
        time.sleep(0.05)
    assert m.results == ["exit code 1"] and "SHIB/USD ABOVE" in h.sent[0][2]


def test_run_live_loop_wiring_survives_a_failing_check(monkeypatch):
    """main(): the check is ticked every loop iteration in its own try/except, after the cycle;
    a check that raises never stops run_cycle, the export, the heartbeat or the sleep."""
    calls = []

    class _Stop(BaseException):
        pass

    class _ExplodingMonitor:
        def tick(self, cfg):
            calls.append("check")
            raise RuntimeError("check exploded")

    cfg = SimpleNamespace(mode="paper", exchange_id="kraken", symbols=["BTC/USD"], loop={"poll_interval_seconds": 0},
                          healthcheck_ping_url=None)
    monkeypatch.setattr(run_live, "load_config", lambda: cfg)
    monkeypatch.setattr(run_live, "build_components", lambda c: (None,) * 6)
    monkeypatch.setattr(run_live, "build_market_data", lambda c, e: None)
    monkeypatch.setattr(run_live, "run_cycle", lambda *a: calls.append("cycle"))
    monkeypatch.setattr(run_live, "_export_and_push", lambda: calls.append("export"))
    monkeypatch.setattr(run_live, "_deploy_dashboard", lambda: None)
    monkeypatch.setattr(run_live, "send_heartbeat_ping", lambda url: calls.append("ping"))
    monkeypatch.setattr(run_live, "run_log_retention", lambda c: None)
    monkeypatch.setattr(run_live, "PairMinimumsMonitor", _ExplodingMonitor)
    monkeypatch.setattr(run_live.dashboard_deploy, "is_enabled", lambda: False)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise _Stop()

    monkeypatch.setattr(run_live.time, "sleep", sleep)
    with pytest.raises(_Stop):
        run_live.main()
    assert calls == ["cycle", "export", "ping", "check"] * 2
