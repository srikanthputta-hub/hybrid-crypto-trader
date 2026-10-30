"""Issue #99: everything live-only stays unreachable in paper mode.

* A fresh interpreter (this pytest process has long since imported src.broker_live through other
  tests) builds the paper bot from the shipped config and runs real run_cycle ticks with a fake
  exchange -- even with LIVE_TRADING_CONFIRM set -- and src.broker_live must never be imported.
* No GitHub workflow sets LIVE_TRADING_CONFIRM, so `mode: paper` is never the only switch
  standing between a CI job and the live broker.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

_SCRIPT = r"""
import json, sys
from pathlib import Path
from src.bot import run_cycle
from src.config import load_config
from tests import test_bot_universe_wiring as w

tmp = Path(sys.argv[1])
raw = w._isolated_raw(w._pre_trial_hourly_raw(load_config().raw), tmp)
assert raw["mode"] == "paper"
symbols = list(raw["market"]["symbols"])
hourly = {s: w._random_walk_hourly(seed=i, start_price=10.0 * (i + 1)) for i, s in enumerate(symbols)}
daily = {s: w._daily(10.0 * (i + 1), uptrend=(i % 4 != 3)) for i, s in enumerate(symbols)}
fake = w.FakeCcxt(hourly, daily)
cfg, parts, events = w._components(raw, fake)
for bar in range(w.FIRST_TICK_BAR, w.LAST_TICK_BAR + 1):
    w._tick_to(fake, bar)
    run_cycle(cfg, *parts)
print(json.dumps({
    "broker": type(parts[3]).__name__,
    "live_loaded": "src.broker_live" in sys.modules,
    "live_confirm_env": cfg.live_trading_confirmed,
    "events": len(events),
    "executions": sum("execution" in e for e in events),
}))
"""


def test_paper_mode_never_imports_the_live_broker(tmp_path):
    env = {**os.environ, "LIVE_TRADING_CONFIRM": "YES-I-UNDERSTAND-THE-RISK", "PYTHONPATH": str(REPO)}
    for key in ("EXCHANGE_API_KEY", "EXCHANGE_API_SECRET", "EXCHANGE_API_PASSWORD"):
        env.pop(key, None)
    out = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(tmp_path)], cwd=REPO, env=env, capture_output=True, text=True,
        timeout=600,
    )
    assert out.returncode == 0, out.stderr[-3000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["broker"] == "PaperBroker"
    assert result["live_confirm_env"] is True  # the env switch alone is on...
    assert result["live_loaded"] is False      # ...and the live broker module was still never imported
    assert result["executions"] > 0            # not vacuous: the paper bot really traded
    assert not (tmp_path / "live_order_state.json").exists() and not (tmp_path / "live_broker_halt.json").exists()


_LIVE_IMPORT = re.compile(r"^\s*(from\s+src\.broker_live\s+import|import\s+src\.broker_live|"
                          r"from\s+src\s+import\s+.*\bbroker_live\b|import\s+broker_live|from\s+broker_live\s+import)")


def _live_imports(text: str) -> list[int]:
    return [i for i, line in enumerate(text.splitlines()) if _LIVE_IMPORT.search(line)
            or ("importlib" in line and "broker_live" in line)]


@pytest.mark.parametrize("line", ["from src.broker_live import LiveBroker", "import src.broker_live",
                                  "import src.broker_live as bl", "from src import broker_live",
                                  "from src import bot, broker_live", "    import broker_live",
                                  "importlib.import_module('src.broker_live')"])
def test_the_static_check_recognises_every_import_form(line):
    assert _live_imports(line)


def test_broker_live_is_imported_only_inside_the_live_branch():
    bot = (REPO / "src" / "bot.py").read_text()
    imports = _live_imports(bot)
    assert len(imports) == 1
    assert bot.splitlines()[imports[0] - 1].strip() == 'if cfg.mode == "live":'
    for path in (REPO / "src").glob("*.py"):
        if path.name not in ("bot.py", "broker_live.py"):
            assert not _live_imports(path.read_text()), path.name


def _workflows():
    root = REPO / ".github" / "workflows"
    return sorted(list(root.glob("*.yml")) + list(root.glob("*.yaml")))


def test_no_workflow_sets_the_live_trading_confirmation():
    assert _workflows()
    for workflow in _workflows():
        for line in workflow.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            assert "LIVE_TRADING_CONFIRM" not in line, f"{workflow.name}: {line.strip()}"


def test_paper_dashboard_job_gets_no_exchange_credentials():
    # review S2: the paper run only reads public market data
    text = (REPO / ".github" / "workflows" / "refresh-dashboard.yml").read_text()
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("EXCHANGE_API" in line for line in code)
