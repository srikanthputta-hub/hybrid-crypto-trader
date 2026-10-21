"""Review fixes R1 / R3 (Architect re-verification of 7095cb35): the official-run guards, exercised
in a REAL temporary git repository with a real bare `origin` (no stubbed code_commit):

  R1  the code label is computed once, before any output exists, and every family record and report
      of an official 3-family run carries the same clean label -- even though family 1's own official
      outputs (docs/intraday_p2_family1.md, strategy_pack_run/intraday_p2_results.json) make the tree
      dirty from family 2 on.
  R3a the SHA-256 of every harness source file is in the results JSON and every report header.
  R3b an official run refuses unless HEAD equals origin/main's tip after a fetch.

The real evaluate_family runs (synthetic data presented as provider kind "real" inside the temp
repo only); integrity_problems is forced to fail so each family stops right after its first base
run with an integrity-only VOID record -- no result is computed.

Run: pytest strategy_pack_run/test_intraday_official_run.py -q
"""
from __future__ import annotations

import functools
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import intraday_data as idata  # noqa: E402
import intraday_synthetic as syn  # noqa: E402
import run_intraday_p2 as p2  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for rel in ("strategy_pack_run/intraday_runner.py", "strategy_pack_run/run_intraday_p2.py",
                "strategy_pack_run/resample_ohlcv.py", "src/risk_engine.py", "src/monitor.py"):
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_REPO_ROOT / rel, work / rel)
    (work / "docs").mkdir()
    (work / "docs" / "intraday_prereg.md").write_text("prereg\n")
    (work / "config").mkdir()
    (work / "config" / "config.yaml").write_text("x: 1\n")
    (work / ".gitignore").write_text("strategy_pack_run/intraday_p2_logs/\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "init")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "origin", "main")
    return work


@functools.lru_cache(maxsize=None)
def _data(tf: str):
    coins = [c for c in idata.UNIVERSE_A if c != "BTC"]
    if tf == "1h":
        return syn.universe_1h(coins, "2020-09-01", 24 * 2240, seed=3, sigma=0.006)
    return syn.universe_15m([c for c in coins if c != "XTZ"], "2021-09-01", 96 * 40, seed=3)


def _fake_real_provider():
    prov = p2.SyntheticProvider(lambda u, tf: _data(tf))
    prov.kind = "real"  # only so the official-mode guard accepts it inside the temp repo
    return prov


def _official(work: Path, monkeypatch):
    monkeypatch.setattr(p2, "_REPO_ROOT", work)  # any code_commit() re-evaluation would look at the temp repo
    monkeypatch.setattr(p2, "integrity_problems", lambda *a, **k: ["forced stop (test)"])
    return p2.run([1, 2, 3], True, 1, provider=_fake_real_provider(),
                  out_json=work / "strategy_pack_run" / "intraday_p2_results.json", docs_dir=work / "docs",
                  logs_dir=work / "strategy_pack_run" / "intraday_p2_logs", repo_root=work)


def test_r1_every_family_of_an_official_run_carries_the_same_clean_label(tmp_path, monkeypatch):
    work = _make_repo(tmp_path)
    head = git(work, "rev-parse", "HEAD")
    rec = _official(work, monkeypatch)
    assert rec["code_commit"] == head and rec["run_kind"] == "OFFICIAL" and rec["complete"] is True
    on_disk = json.loads((work / "strategy_pack_run" / "intraday_p2_results.json").read_text())
    for f in (1, 2, 3):
        r = on_disk["results"][f"family{f}"]
        assert r["code_commit"] == head, (f, r["code_commit"])
        assert r["verdict"] == "VOID" and "grid" not in r
        rep = (work / "docs" / f"intraday_p2_family{f}.md").read_text()
        assert f"code `{head}`" in rep and "+dirty" not in rep
    # the condition the fix guards against really occurred: after family 1 the tree WAS dirty
    assert p2.code_commit(work).endswith("+dirty")


def test_r3a_source_hashes_are_recorded_in_the_json_and_every_report(tmp_path, monkeypatch):
    work = _make_repo(tmp_path)
    rec = _official(work, monkeypatch)
    src = rec["source_sha256"]
    assert set(src) == {"strategy_pack_run/intraday_runner.py", "strategy_pack_run/run_intraday_p2.py",
                        "strategy_pack_run/resample_ohlcv.py", "src/risk_engine.py", "src/monitor.py"}
    assert src["src/risk_engine.py"] == p2._sha(_REPO_ROOT / "src" / "risk_engine.py")
    for f in (1, 2, 3):
        rep = (work / "docs" / f"intraday_p2_family{f}.md").read_text()
        assert all(f"`{k}` {v}" in rep for k, v in src.items())


def test_r3a_live_source_hashes_cover_every_harness_file():
    got = p2.source_hashes()
    for name in ("intraday_data.py", "intraday_families.py", "intraday_runner.py", "intraday_stats.py",
                 "run_intraday_p2.py", "run_intraday_prereg.py", "resample_ohlcv.py"):
        assert f"strategy_pack_run/{name}" in got
    assert "src/risk_engine.py" in got and "src/monitor.py" in got


def test_r3b_official_run_refuses_unless_head_is_origin_main_tip(tmp_path, monkeypatch):
    work = _make_repo(tmp_path)
    tip = git(work, "rev-parse", "HEAD")
    (work / "docs" / "note.md").write_text("local only\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "not pushed")
    head = git(work, "rev-parse", "HEAD")
    monkeypatch.setattr(p2, "evaluate_family", lambda *a, **k: pytest.fail("must not evaluate"))
    with pytest.raises(SystemExit) as exc:
        p2.run([1, 2, 3], True, 1, provider=_fake_real_provider(), out_json=tmp_path / "o.json", docs_dir=tmp_path,
               logs_dir=tmp_path, repo_root=work)
    msg = str(exc.value)
    assert head in msg and tip in msg
    assert not (tmp_path / "o.json").exists()


def test_r3b_official_run_refuses_without_an_origin_main(tmp_path, monkeypatch):
    work = _make_repo(tmp_path)
    git(work, "remote", "remove", "origin")
    with pytest.raises(SystemExit) as exc:
        p2.run([1, 2, 3], True, 1, provider=_fake_real_provider(), out_json=tmp_path / "o.json", docs_dir=tmp_path,
               logs_dir=tmp_path, repo_root=work)
    assert "origin/main unavailable" in str(exc.value)


def test_r1_evaluate_family_never_recomputes_the_label_when_given(monkeypatch):
    monkeypatch.setattr(p2, "code_commit", lambda *a, **k: pytest.fail("label must not be recomputed"))
    monkeypatch.setattr(p2, "integrity_problems", lambda *a, **k: ["forced stop (test)"])
    r = p2.evaluate_family(1, _fake_real_provider(), workers=1, n_placebo=1, verbose=False, code_label="abc123")
    assert r["code_commit"] == "abc123"
