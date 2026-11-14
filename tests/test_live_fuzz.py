"""Bounded, deterministic-seed versions of the two review fuzz harnesses (issue #99 review round):

* tests/_fuzz_arch.py (Architect): SIGKILL-style crashes and I/O errors injected before/after every
  persistence step (order record, halt marker, trades.csv, plan store, history) and around the
  exchange accepting the order; rebuild from disk after every crash. Invariants: no duplicate buy
  per plan, no clientOrderId accepted twice, no trades.csv double row, no halt lost across a restart,
  no plan created while halted, and -- when not halted and no record is open -- ledger == exchange.
* tests/_fuzz_rr.py (Risk Officer): exchange faults (timeouts before/after, garbage / partial /
  zero-fill responses, fees in base/quote/BNB, lookup + balance failures), disk faults (OSError and
  crashes around every write), and both together, behind the REAL ExchangeClient. Invariants: no
  double booking, no over-booking that the broker did not itself flag (unverified + halted), no
  duplicate buy, no id accepted twice, no halt marker disappearing, no silent ledger divergence.

Before the review fixes (2168b7ae) the Risk Officer harness found violations in 24 of 180 episodes
(OVER-BOOKED x24, in-memory ledger x3) with exactly these seeds; after them: none.
"""
from __future__ import annotations

import collections
import logging

import pytest

import src.broker_live as broker_live
import tests._fuzz_arch as fuzz_arch
import tests._fuzz_rr as fuzz_rr


@pytest.fixture(autouse=True)
def _quiet_and_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(broker_live, "RECONCILE_NOT_FOUND_GRACE_SECONDS", 0)
    counter = iter(range(10**6))
    monkeypatch.setattr(fuzz_arch.tempfile, "mkdtemp",
                        lambda prefix="": str((tmp_path / f"{prefix}{next(counter)}").mkdir() or
                                              tmp_path / f"{prefix}{next(counter) - 1}"))
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.mark.parametrize(("crash_p", "err_p"), [(0.04, 0.0), (0.03, 0.02), (0.08, 0.04)])
def test_architect_crash_fuzz_has_no_violations(crash_p, err_p):
    total = collections.Counter()
    examples = []
    restarts = orders = memory_only = 0
    for seed in range(300):
        violations, details, r, o = fuzz_arch.run_seed(seed, crash_p=crash_p, err_p=err_p, clean_w=3)
        restarts += r
        orders += o
        memory_only += fuzz_arch.run_seed.last_memory_only_lost
        total.update(violations)
        examples += [(seed, d) for d in details][:2]
    # not vacuous: measured 300 seeds -> ~500-800 exchange orders and ~500-1000 crash/restarts per setting
    assert orders > 300 and restarts > 300
    assert not total, (dict(total), examples[:5])
    # Residual (documented): a halt whose every write after it failed or crashed is lost with the
    # process. It needs >= 2 consecutive persistence faults; never with crashes alone.
    if err_p == 0:
        assert memory_only == 0
    print(f"crash_p={crash_p} err_p={err_p}: orders={orders} restarts={restarts} memory-only halts lost={memory_only}")


@pytest.mark.parametrize("profile", sorted(fuzz_rr.PROFILES))
def test_risk_officer_fault_fuzz_has_no_violations(profile):
    violations = []
    stats = collections.Counter()
    for seed in range(60):
        res = fuzz_rr.run_episode(seed, 25, fuzz_rr.PROFILES[profile])
        violations += [(seed, v) for v in res["violations"]]
        stats.update(res["stats"])
    assert stats["plans"] > 100 and stats["halted_end"] > 0  # not vacuous
    assert not violations, violations[:5]
