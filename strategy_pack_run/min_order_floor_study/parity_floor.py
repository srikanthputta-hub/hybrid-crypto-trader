"""Scratch driver: live loop (bar_parity) vs P6 harness, 0.10% paper fee, real opens, 200-bar
history, same min_order_quote_usd on both sides. Uses tests/test_daily_live_parity.py's
run_harness / run_bot unmodified, from an extracted copy of claude/bot-bar-parity @ 472bdc0.

usage: python parity_floor.py <parity_src> <data_subdir> <floor> <out.json> [n_bars]
  <parity_src>: an extracted copy of claude/bot-bar-parity @ 472bdc0
  (git -C <repo> archive 472bdc0 | tar -x -C <parity_src>); n_bars 719 on data/ = the parity
  doc's full run. Information for docs/min_order_floor_study.md; not runnable on main alone.
"""
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

src_root, data_sub, floor, out = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
N_BARS = int(sys.argv[5]) if len(sys.argv) > 5 else 0  # 719 on data/ = the parity doc's full run
sys.path.insert(0, src_root)
os.chdir(src_root)

import pytest  # noqa: E402

import src.bot as bot  # noqa: E402
from src import htf_filter  # noqa: E402
from tests import test_daily_live_parity as P  # noqa: E402
from tests.test_bot_universe_wiring import _FakeDatetime  # noqa: E402


def load_rows(data_dir: Path):
    rows = {}
    for path in sorted(data_dir.glob("*_1d.csv")):
        base, quote = path.stem.split("_")[:2]
        df = pd.read_csv(path)
        rows[f"{base}/{quote}"] = [
            [int(x[0])] + [float(v) for v in x[1:]]
            for x in df[["timestamp", "open", "high", "low", "close", "volume"]].values.tolist()
        ]
    axis = sorted({x[0] for rs in rows.values() for x in rs})
    if N_BARS:
        axis = axis[:N_BARS]
        rows = {s: [x for x in rs if x[0] <= axis[-1]] for s, rs in rows.items()}
        rows = {s: rs for s, rs in rows.items() if rs}
    return rows, axis


rows, axis = load_rows(Path(src_root) / "strategy_pack_run" / data_sub)
tmp = Path(out).with_suffix("") .parent / (Path(out).stem + "_tmp")
tmp.mkdir(parents=True, exist_ok=True)
t_start = time.time()
mp = pytest.MonkeyPatch()
try:
    mp.setattr(htf_filter, "_cache", {})
    mp.setattr("src.config.DEFAULT_BAR_DECISIONS_PATH", str(tmp / "default_bar_decisions.json"))
    mp.setattr("src.risk_engine.datetime", _FakeDatetime)
    bot._reset_bar_parity_state()
    h_checks, h_exits, res = P.run_harness(rows, fee=0.001, min_order=floor, tmp=tmp / "h")
    b_checks, b_exits, _, _, _ = P.run_bot(rows, axis, fee=0.001, min_order=floor, history_bars=200, tmp=tmp / "b", monkeypatch=mp)
finally:
    bot._reset_bar_parity_state()
    mp.undo()

H = {(c["bar"], c["symbol"]): c for c in h_checks}
B = {(c["bar"], c["symbol"]): c for c in b_checks}
h_ent = {k for k, c in H.items() if c["allowed"]}
b_ent = {k for k, c in B.items() if c["allowed"]}
live_only = sorted(b_ent - h_ent)
harness_only = sorted(h_ent - b_ent)


def cat(c):
    if c is None:
        return "no harness check"
    r = c["reason"]
    if c["allowed"]:
        return "approved"
    if r.startswith(P.NO_ROOM):
        return "no_room"
    if P.BELOW_MIN in r:
        return "below_min"
    return r.split(":")[0][:40]


live_only_detail = [
    dict(bar=str(k[0].date()), symbol=k[1], live_quote=B[k]["quote"], harness=cat(H.get(k)))
    for k in live_only
]
fee_slack = [d for d in live_only_detail if d["harness"] in ("no_room", "below_min")]
first = next((k for k in sorted(set(H) | set(B)) if k not in H or k not in B or H[k]["allowed"] != B[k]["allowed"]), None)
result = dict(
    data=data_sub, floor=floor, bars=len(axis), seconds=round(time.time() - t_start),
    harness_checks=len(H), live_checks=len(B), harness_entries=len(h_ent), live_entries=len(b_ent),
    common_entries=len(h_ent & b_ent),
    entry_mismatch=len(h_ent ^ b_ent) / max(1, len(h_ent)),
    harness_exits=len(h_exits), live_exits=len(b_exits),
    exit_mismatch=len(h_exits ^ b_exits) / max(1, len(h_exits)),
    live_only_n=len(live_only), harness_only_n=len(harness_only),
    fee_slack_live_only_n=len(fee_slack),
    fee_slack_max_quote=max((d["live_quote"] for d in fee_slack), default=None),
    live_only_detail=live_only_detail[:40],
    harness_only_detail=[dict(bar=str(k[0].date()), symbol=k[1], quote=H[k]["quote"], live=cat(B.get(k))) for k in harness_only][:40],
    first_divergence=None if first is None else dict(
        bar=str(first[0].date()), symbol=first[1],
        harness=None if first not in H else dict(allowed=H[first]["allowed"], quote=H[first]["quote"], reason=H[first]["reason"][:120]),
        live=None if first not in B else dict(allowed=B[first]["allowed"], quote=B[first]["quote"], reason=B[first]["reason"][:120]),
    ),
    harness_trades=int(len(res.trades)),
)
Path(out).write_text(json.dumps(result, indent=1, default=str))
print(json.dumps({k: v for k, v in result.items() if not k.endswith("detail")}, default=str))
