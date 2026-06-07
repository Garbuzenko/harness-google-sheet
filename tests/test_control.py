"""Offline tests for the `_control` intent queue + dispatcher.

No Google, no claude. Run: pytest -q

These tests mutate the module-global CONTROL_HANDLERS and orchestrator._STOP;
the `clean_registry` fixture snapshots/restores both so tests never leak.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheet_agent import config as C
from sheet_agent import orchestrator as orch_mod
from sheet_agent.orchestrator import Orchestrator, _now, register_control_handler
from sheet_agent.sheets import MockBackend, parse_control_grid


@pytest.fixture(autouse=True)
def clean_registry():
    """Snapshot/restore the global handler registry and the _STOP flag so a test
    that registers a handler (or sets _STOP) can't poison the rest of the suite."""
    saved = dict(orch_mod.CONTROL_HANDLERS)
    saved_stop = orch_mod._STOP
    orch_mod.CONTROL_HANDLERS.clear()
    orch_mod._STOP = False
    try:
        yield
    finally:
        orch_mod.CONTROL_HANDLERS.clear()
        orch_mod.CONTROL_HANDLERS.update(saved)
        orch_mod._STOP = saved_stop


def _mock_orch(tmp_path: Path, **kw) -> Orchestrator:
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"), **kw)
    return Orchestrator(cfg)


def _control_grid(mock_path: Path) -> list[list[str]]:
    return json.loads(Path(mock_path).read_text())["tabs"][C.CONTROL_TAB]["grid"]


def _seed_control_row(be: MockBackend, row: int, *, cid="", ts="", action="",
                      args="", status="", result="") -> None:
    """Write a full A-F control row directly via write_cell (Apps-Script style)."""
    be.ensure_control_schema()
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_ID, cid)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_TS, ts)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_ACTION, action)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_ARGS, args)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_STATUS, status)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_RESULT, result)


# --- AC: constants ---------------------------------------------------------
def test_control_constants():
    assert C.CONTROL_TAB == "_control"
    assert C.CONTROL_TAB.startswith(C.META_PREFIX) is True
    assert C.CONTROL_HEADERS == ["id", "ts", "action", "args", "status", "result"]


# --- AC: read_control returns rows oldest-first ----------------------------
def test_read_control_oldest_first(tmp_path: Path):
    be = MockBackend(str(tmp_path / "m.json"))
    # Seed out of ts order; oldest ts must come back first.
    _seed_control_row(be, 2, cid="b", ts="2026-06-06 12:00:00Z", action="x",
                      args="{}", status=C.CTL_PENDING)
    _seed_control_row(be, 3, cid="a", ts="2026-06-06 09:00:00Z", action="x",
                      args="{}", status=C.CTL_PENDING)
    _seed_control_row(be, 4, cid="c", ts="2026-06-06 15:00:00Z", action="x",
                      args="{}", status=C.CTL_PENDING)
    rows = be.read_control()
    assert [r.id for r in rows] == ["a", "b", "c"]


def test_parse_control_grid_blank_ts_falls_back_to_row():
    grid = [
        ["id", "ts", "action", "args", "status", "result"],
        ["first", "", "x", "{}", "pending", ""],
        ["second", "", "x", "{}", "pending", ""],
    ]
    rows = parse_control_grid(grid)
    assert [r.id for r in rows] == ["first", "second"]  # row order preserved


# --- AC: ensure_control_schema idempotent ----------------------------------
def test_ensure_control_schema_idempotent(tmp_path: Path):
    mock_path = tmp_path / "m.json"
    be = MockBackend(str(mock_path))
    be.ensure_control_schema()
    be.ensure_control_schema()  # second call must add nothing
    grid = _control_grid(mock_path)
    assert grid[0] == C.CONTROL_HEADERS            # exactly one header row
    assert len(grid) == 1                           # no data rows added
    assert be.read_control() == []                  # no rows parsed


# --- AC: dispatcher writes ONLY E/F; A-D byte-for-byte unchanged ------------
def test_dispatch_leaves_a_to_d_untouched(tmp_path: Path):
    mock_path = tmp_path / "m.json"
    orch = _mock_orch(tmp_path)
    be = orch.backend
    register_control_handler("noop", lambda o, cr, a: "ok")
    _seed_control_row(be, 2, cid="id-1", ts="2026-06-06 10:00:00Z",
                      action="noop", args='{"k": "v"}', status=C.CTL_PENDING)
    before = _control_grid(mock_path)[1][:4]        # A-D snapshot

    orch._process_control()

    after_grid = _control_grid(mock_path)
    assert after_grid[1][:4] == before              # A-D byte-for-byte identical
    assert after_grid[1][C.COL_CTL_STATUS - 1] == C.CTL_DONE   # E changed
    assert after_grid[1][C.COL_CTL_RESULT - 1] == "ok"         # F changed


# --- AC: idempotency by id — done/error rows are skipped --------------------
def test_idempotency_done_row_not_reinvoked(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    be = orch.backend
    calls: list[str] = []
    register_control_handler("spy", lambda o, cr, a: calls.append(cr.id) or "ran")
    _seed_control_row(be, 2, cid="done-1", ts="2026-06-06 10:00:00Z",
                      action="spy", args="{}", status=C.CTL_DONE,
                      result="original result")

    orch._process_control()

    assert calls == []                               # handler NOT re-invoked
    rows = {r.id: r for r in be.read_control()}
    assert rows["done-1"].result == "original result"   # F not overwritten
    assert rows["done-1"].status == C.CTL_DONE


def test_idempotency_error_row_not_reinvoked(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    be = orch.backend
    calls: list[str] = []
    register_control_handler("spy", lambda o, cr, a: calls.append(cr.id) or "ran")
    _seed_control_row(be, 2, cid="err-1", ts="2026-06-06 10:00:00Z",
                      action="spy", args="{}", status=C.CTL_ERROR,
                      result="prior error")
    orch._process_control()
    assert calls == []
    rows = {r.id: r for r in be.read_control()}
    assert rows["err-1"].result == "prior error"


# --- AC: stale-reclaim parity ----------------------------------------------
def test_stale_working_reclaimed_fresh_untouched(tmp_path: Path):
    orch = _mock_orch(tmp_path, agent_timeout=1)     # grace = max(2, 600) = 600s
    be = orch.backend
    # Stale: ts well older than the grace window.
    _seed_control_row(be, 2, cid="stale", ts="2020-01-01 00:00:00Z",
                      action="x", args="{}", status=C.CTL_WORKING)
    # Fresh: just now -> must stay working.
    _seed_control_row(be, 3, cid="fresh", ts=_now(),
                      action="x", args="{}", status=C.CTL_WORKING)

    rows = be.read_control()
    orch._reclaim_stale_control(rows)

    after = {r.id: r for r in be.read_control()}
    assert after["stale"].status == C.CTL_PENDING    # reclaimed
    assert after["stale"].result == "reclaimed after crash"
    assert after["fresh"].status == C.CTL_WORKING    # left untouched


def test_stale_reclaim_handles_apps_script_iso_ts(tmp_path: Path):
    """Regression: column B `ts` is an Apps-Script ISO timestamp (`...T...Z`,
    fractional seconds, offsets), NOT the daemon's "%Y-%m-%d %H:%M:%SZ" stamp.
    A fresh ISO `working` row must stay working (not be re-dispatched every cycle)."""
    from datetime import datetime, timezone

    orch = _mock_orch(tmp_path, agent_timeout=1)     # grace = 600s
    be = orch.backend
    fresh_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    # Stale ISO (well past the grace window) -> reclaimed.
    _seed_control_row(be, 2, cid="stale-iso", ts="2020-01-01T00:00:00.000Z",
                      action="x", args="{}", status=C.CTL_WORKING)
    # Fresh ISO (just clicked) -> must stay working.
    _seed_control_row(be, 3, cid="fresh-iso", ts=fresh_iso,
                      action="x", args="{}", status=C.CTL_WORKING)

    orch._reclaim_stale_control(be.read_control())

    after = {r.id: r for r in be.read_control()}
    assert after["stale-iso"].status == C.CTL_PENDING     # reclaimed
    assert after["fresh-iso"].status == C.CTL_WORKING     # NOT re-dispatched


# --- AC: daemon never dies — raising handler + malformed args ---------------
def test_daemon_survives_raising_handler_and_bad_args(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    be = orch.backend

    def boom(o, cr, a):
        raise RuntimeError("kaboom")

    spy_calls: list[str] = []
    register_control_handler("boom", boom)
    register_control_handler("spy", lambda o, cr, a: spy_calls.append(cr.id) or "spied")

    # (a) raising handler, (b) malformed JSON args, (c) a following good row.
    _seed_control_row(be, 2, cid="a-boom", ts="2026-06-06 09:00:00Z",
                      action="boom", args="{}", status=C.CTL_PENDING)
    _seed_control_row(be, 3, cid="b-bad", ts="2026-06-06 10:00:00Z",
                      action="spy", args="garbage{", status=C.CTL_PENDING)
    _seed_control_row(be, 4, cid="c-good", ts="2026-06-06 11:00:00Z",
                      action="spy", args="{}", status=C.CTL_PENDING)

    # Must NOT raise.
    orch._process_control()

    rows = {r.id: r for r in be.read_control()}
    assert rows["a-boom"].status == C.CTL_ERROR
    assert "kaboom" in rows["a-boom"].result
    assert rows["b-bad"].status == C.CTL_ERROR
    assert "bad args JSON" in rows["b-bad"].result
    assert rows["c-good"].status == C.CTL_DONE        # later row still processed
    assert spy_calls == ["c-good"]                     # bad-args row never reached handler


def test_run_once_one_cycle_does_not_raise(tmp_path: Path):
    """Drive ONE real supervisor cycle (`run_once` — the exact unit `run_forever`
    loops over) with a poisoned control row, and assert the cycle swallows the crash
    and returns cleanly with the row marked `error`. The `clean_registry` fixture
    resets the module `_STOP` so a flip here can't leak into other tests."""
    orch = _mock_orch(tmp_path, poll_interval=1)
    be = orch.backend
    register_control_handler("boom", lambda o, cr, a: (_ for _ in ()).throw(RuntimeError("x")))
    _seed_control_row(be, 2, cid="boom-1", ts="2026-06-06 10:00:00Z",
                      action="boom", args="{}", status=C.CTL_PENDING)

    assert orch.run_once(drain=True) == 0    # no tabs -> 0 work items, no raise
    rows = {r.id: r for r in be.read_control()}
    assert rows["boom-1"].status == C.CTL_ERROR


# --- AC: unknown action -> error, no crash ---------------------------------
def test_unknown_action_marks_error(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    be = orch.backend
    _seed_control_row(be, 2, cid="u-1", ts="2026-06-06 10:00:00Z",
                      action="nope", args="{}", status=C.CTL_PENDING)

    orch._process_control()  # empty registry -> unknown action

    rows = {r.id: r for r in be.read_control()}
    assert rows["u-1"].status == C.CTL_ERROR
    assert "unknown action 'nope'" in rows["u-1"].result


# --- AC: `doctor` must never stamp the repo grid onto meta tabs -------------
def test_doctor_does_not_corrupt_control_schema(tmp_path: Path):
    """Regression: `doctor` iterated EVERY tab and called read_tab(), which
    bootstraps the repo grid (REPO_PATH/Task headers) on first read.
    Run against the live sheet it clobbered the `_control` header
    (id|ts|action|args|status|result) and the `_repos` list. doctor must skip
    META_PREFIX tabs like every other tab loop."""
    from sheet_agent.__main__ import _doctor

    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"))
    be = Orchestrator(cfg).backend
    be.ensure_control_schema()        # pristine control header
    be.ensure_schema("repo-tab")      # a normal repo tab (must still be readable)

    grid = _control_grid(tmp_path / "m.json")
    assert [h.lower() for h in grid[0][:6]] == [h.lower() for h in C.CONTROL_HEADERS]

    assert _doctor(cfg) == 0           # connects, iterates, reports

    grid = _control_grid(tmp_path / "m.json")
    assert [h.lower() for h in grid[0][:6]] == [h.lower() for h in C.CONTROL_HEADERS], (
        f"doctor corrupted the _control header: {grid[0]!r}")
    # and no stray REPO_PATH/Task rows were injected as control data
    assert grid[0][0].lower() == "id"


# --- background agent execution: non-blocking + serial-per-repo -------------
def test_run_items_non_blocking_and_no_double_dispatch(tmp_path: Path):
    """Agents run in a background pool so the poll loop keeps dispatching `_control`
    button intents while long repo-task agents are mid-flight. A repo already
    in-flight must NOT be re-dispatched (serial within one repo, across cycles)."""
    import threading
    import time
    from sheet_agent.orchestrator import WorkItem
    from sheet_agent.sheets import TaskRow

    orch = _mock_orch(tmp_path)
    started = threading.Event()
    release = threading.Event()
    calls: list[tuple[str, int]] = []

    def fake_process(w: WorkItem) -> None:
        calls.append((str(w.repo_path), w.task.row))
        started.set()
        release.wait(5)

    orch._process_task = fake_process  # type: ignore[method-assign]

    def wi(repo: str, row: int) -> WorkItem:
        return WorkItem(title="t", repo_path=Path(repo), allow_init=False,
                        task=TaskRow(row=row, task="do", status=""),
                        phase="full", spec_id="", next_tries=1)

    t0 = time.monotonic()
    orch._run_items([wi("/r1", 4)])
    assert time.monotonic() - t0 < 1.0, "_run_items must not block on the agent"
    assert started.wait(2), "agent should start in the background pool"

    # same repo dispatched again while in-flight → must be ignored (no 2nd agent)
    orch._run_items([wi("/r1", 5)])
    time.sleep(0.3)
    assert calls == [("/r1", 4)], f"in-flight repo must not re-dispatch, got {calls}"
    assert Path("/r1") in orch._inflight

    release.set()
    orch._pool.shutdown(wait=True)
    assert ("/r1", 4) in calls
    assert Path("/r1") not in orch._inflight, "claim must be released after the group ends"


# --- interrupted agent (daemon restart / SIGTERM) requeues, never fails ------
def test_interrupted_agent_requeues_and_refunds_attempt(tmp_path: Path, monkeypatch):
    """A SIGTERM-killed agent (the daemon was restarted for a deploy) returns no
    JSON. That must NOT mark the task `failed` with 'no structured output' — it is
    requeued and the attempt is refunded so a restart can't burn the retry budget."""
    from sheet_agent import agent as agent_mod
    from sheet_agent.orchestrator import WorkItem
    from sheet_agent.sheets import TaskRow

    orch = _mock_orch(tmp_path)
    orch.backend.ensure_schema("repo")

    monkeypatch.setattr(
        agent_mod, "run",
        lambda *a, **k: agent_mod.AgentResult(outcome="failed", interrupted=True,
                                              error="interrupted (exit=143)"))

    w = WorkItem(title="repo", repo_path=Path(tmp_path), allow_init=False,
                 task=TaskRow(row=4, task="do", status=""), phase="full",
                 spec_id="", next_tries=2)
    orch._process_task(w)

    row4 = next(r for r in orch.backend.read_tab("repo").rows if r.row == 4)
    assert row4.status == C.ST_QUEUED, f"interrupted task must requeue, got {row4.status!r}"
    assert row4.tries == 1, f"the attempt must be refunded (2 -> 1), got {row4.tries}"


# --- Updated/heartbeat timestamps: display tz, but offset-encoded & age-correct
def test_now_encodes_offset_and_age_stays_correct():
    """`_now()` shows the display timezone (Moscow by default) but MUST encode the
    UTC offset, so `_age_seconds` can't be skewed by hours (which would wrongly
    reclaim or never-reclaim 'working' rows)."""
    from sheet_agent.orchestrator import _now, _age_seconds

    s = _now()
    assert ("+" in s) or s.endswith("Z"), f"_now must carry a tz offset, got {s!r}"
    assert _age_seconds(s) < 5, f"round-trip age must be ~0s, got {_age_seconds(s)}"
