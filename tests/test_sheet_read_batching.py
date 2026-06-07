"""Offline tests for the per-cycle batched sheet read (quota optimization): one
`read_all` snapshot per sheet per cycle, reads served from it, live fallback when no
cycle is active.

Run: pytest -q -k read_batching
"""
from __future__ import annotations

from pathlib import Path

from sheet_agent import config as C
from sheet_agent.orchestrator import Orchestrator
from sheet_agent.sheets import MockBackend


def _mock_orch(tmp_path: Path, **kw) -> Orchestrator:
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"), **kw)
    return Orchestrator(cfg)


def _repo(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    (p / "openspec").mkdir(parents=True, exist_ok=True)
    return p


def _bind(be: MockBackend, title: str, binding: str) -> None:
    be.write_cell(title, C.CONFIG_ROW, C.COL_REPO_BINDING, binding)
    be.ensure_schema(title)


def _friend_be(tmp_path: Path, sid: str) -> MockBackend:
    return MockBackend(str(tmp_path / f"friend-{sid}.json"))


# --- read_all returns every tab's grid in one shot --------------------------
def test_read_all_returns_all_grids(tmp_path: Path):
    be = MockBackend(str(tmp_path / "m.json"))
    be.write_cell("a", 1, 1, "x")
    be.write_cell("b", 2, 2, "y")
    snap = be.read_all()
    assert snap["a"][0][0] == "x"
    assert snap["b"][1][1] == "y"
    assert set(snap) == {"a", "b"}


# --- reads are served from the snapshot, not re-read ------------------------
def test_reads_served_from_snapshot_then_live_after_end(tmp_path: Path):
    be = MockBackend(str(tmp_path / "m.json"))
    _bind(be, "t", "repo-a")
    be.write_cell("t", C.FIRST_TASK_ROW, C.COL_TASK, "original")

    be.begin_cycle()
    # A live write during the cycle does NOT change what reads see (snapshot isolation).
    be.write_cell("t", C.FIRST_TASK_ROW, C.COL_TASK, "changed")
    assert be.read_tab("t").rows[0].task == "original"
    # list_tab_titles is also served from the snapshot.
    assert "t" in be.list_tab_titles()

    be.end_cycle()
    # After the cycle, reads go live again and see the write.
    assert be.read_tab("t").rows[0].task == "changed"


# --- with no cycle active, reads fall back to live (backward compatible) -----
def test_live_fallback_when_no_cycle(tmp_path: Path):
    be = MockBackend(str(tmp_path / "m.json"))
    _bind(be, "t", "repo-a")
    be.write_cell("t", C.FIRST_TASK_ROW, C.COL_TASK, "v1")
    assert be.read_tab("t").rows[0].task == "v1"      # no begin_cycle → live
    be.write_cell("t", C.FIRST_TASK_ROW, C.COL_TASK, "v2")
    assert be.read_tab("t").rows[0].task == "v2"      # sees it immediately


# --- run_once snapshots each sheet exactly once -----------------------------
def test_run_once_one_snapshot_per_sheet(tmp_path: Path):
    orch = _mock_orch(tmp_path, autonomy="ship")
    # Master repo tab (no task → no dispatch, keeps the test about reads only).
    _bind(orch.backend, "mtab", str(_repo(tmp_path, "mrepo")))
    # A registered friend sheet with its own repo tab.
    fbe = _friend_be(tmp_path, "FID")
    _bind(fbe, "ftab", str(_repo(tmp_path, "frepo")))
    orch.backend.append_friend("FID", [str(tmp_path / "frepo")], "p@x.com", "gated", "u")

    # Count read_all on each sheet's backend.
    mcount = [0]
    m_real = orch.backend.read_all
    orch.backend.read_all = lambda: (mcount.__setitem__(0, mcount[0] + 1) or m_real())
    fcount = [0]
    f_real = fbe.read_all
    fbe.read_all = lambda: (fcount.__setitem__(0, fcount[0] + 1) or f_real())
    orch._friend_backends["FID"] = fbe   # daemon uses our counting friend backend

    orch.run_once(drain=True)

    assert mcount[0] == 1   # exactly one batched read for the master sheet
    assert fcount[0] == 1   # exactly one batched read for the friend sheet


# --- a failed master snapshot skips the poll, never crashes -----------------
def test_failed_master_snapshot_skips_poll(tmp_path: Path):
    orch = _mock_orch(tmp_path, autonomy="ship")
    _bind(orch.backend, "mtab", str(_repo(tmp_path, "mrepo")))
    orch.backend.write_cell("mtab", C.FIRST_TASK_ROW, C.COL_TASK, "do x")
    orch.backend.write_cell("mtab", C.FIRST_TASK_ROW, C.COL_STATUS, C.ST_QUEUED)

    def _boom():
        raise RuntimeError("429")
    orch.backend.read_all = _boom

    n = orch.run_once(drain=True)   # must not raise

    assert n == 0   # nothing dispatched this cycle
    # The task is untouched (still queued), to be picked up when the snapshot recovers.
    # (read_tab serves from live JSON via _grid_cells, independent of read_all.)
    assert orch.backend.read_tab("mtab").rows[0].status == C.ST_QUEUED
