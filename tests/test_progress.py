"""Offline tests for live progress — no Google, no real claude.

The streaming `agent.run` is exercised against a fake `CLAUDE_BIN` shell script
that emits a stream-json transcript, exactly as the repo policy requires.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from sheet_agent import config as C
from sheet_agent import agent
from sheet_agent.agent import (
    Progress,
    ProgressTracker,
    _STAGE_INDEX,
    format_progress,
)
from sheet_agent.orchestrator import Orchestrator
from sheet_agent.sheets import TaskRow
from sheet_agent.orchestrator import WorkItem


# --------------------------------------------------------------------------
# ProgressTracker — pure unit tests
# --------------------------------------------------------------------------
def _tool_event(name: str, **inp) -> dict:
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def test_tracker_advances_through_the_pipeline():
    tr = ProgressTracker("ship", "full")
    seq = [
        _tool_event("Bash", command="openspec new change foo"),
        _tool_event("Write", file_path="/r/src/x.py"),
        _tool_event("Bash", command="python -m pytest -q"),
        _tool_event("Bash", command="git commit -m x"),
        _tool_event("Bash", command="git push origin HEAD"),
        _tool_event("Bash", command="bash deploy/deploy.sh"),
    ]
    stages, pcts = [], []
    for e in seq:
        tr.observe(e)
        s = tr.snapshot()
        stages.append(s.stage)
        pcts.append(s.pct)
    assert stages == ["spec", "implement", "tests", "commit", "push", "deploy"]
    assert pcts == sorted(pcts)              # monotonic
    assert all(p < 100 for p in pcts)        # never 100 until the run ends


def test_deploy_stage_anchors_to_real_invocations():
    cc = agent._classify_command
    # Real deploy invocations classify as the terminal `deploy` stage.
    assert cc("bash deploy/deploy.sh") == "deploy"
    assert cc("./deploy.sh") == "deploy"
    assert cc("docker compose up -d") == "deploy"
    # But a different script that merely ends in `deploy.sh` must NOT false-positive.
    assert cc("bash scripts/predeploy.sh") != "deploy"
    assert cc("sh predeploy.sh") != "deploy"
    assert cc("bash redeploy.sh") != "deploy"
    # And a bare mention is not a deploy either.
    assert cc("cat deploy.sh") is None


def test_tracker_never_regresses_on_out_of_order_signals():
    tr = ProgressTracker("ship", "full")
    tr.observe(_tool_event("Bash", command="python -m pytest"))
    s1 = tr.snapshot()
    # A late spec edit must NOT roll the stage back to `spec`.
    tr.observe(_tool_event("Bash", command="openspec validate add-x"))
    s2 = tr.snapshot()
    assert _STAGE_INDEX[s2.stage] >= _STAGE_INDEX[s1.stage]
    assert s2.pct >= s1.pct


def test_percent_ladder_scales_to_terminal_stage():
    # spec-only run: reaching `spec` is nearly the whole job.
    spec_tr = ProgressTracker("spec", "full")
    for _ in range(8):
        spec_tr.observe(_tool_event("Bash", command="openspec validate"))
    assert spec_tr.snapshot().stage == "spec"
    assert spec_tr.snapshot().pct >= 80

    # ship run: the same spec work is only a small slice of the pipeline.
    ship_tr = ProgressTracker("ship", "full")
    for _ in range(8):
        ship_tr.observe(_tool_event("Bash", command="openspec validate"))
    assert ship_tr.snapshot().stage == "spec"
    assert ship_tr.snapshot().pct < 30


def test_implement_phase_starts_past_spec():
    tr = ProgressTracker("ship", "implement")
    s = tr.snapshot()
    assert s.stage == "implement"
    assert s.pct >= 15           # the (approved) spec already counts as done


def test_unclassified_tool_calls_still_show_life():
    tr = ProgressTracker("ship", "full")
    tr.observe(_tool_event("Bash", command="openspec new change foo"))
    p0 = tr.snapshot().pct
    # A plain read/grep (no stage signal) should creep the percent within `spec`.
    tr.observe(_tool_event("Bash", command="ls -la"))
    tr.observe(_tool_event("Bash", command="cat README.md"))
    p1 = tr.snapshot().pct
    assert tr.snapshot().stage == "spec"
    assert p1 >= p0


def test_format_progress_carries_stage_pct_and_mode():
    s = format_progress(Progress(stage="implement", pct=58, autonomy="ship", phase="full"))
    assert "implement" in s and "58" in s and "ship" in s
    # an approved implement run advertises that it will ship
    s2 = format_progress(Progress(stage="implement", pct=20, autonomy="gated", phase="implement"))
    assert "approved" in s2
    # gated spec phase shows it parks for a human
    s3 = format_progress(Progress(stage="spec", pct=40, autonomy="gated", phase="spec"))
    assert "gated" in s3


# --------------------------------------------------------------------------
# Streaming agent.run against a fake claude
# --------------------------------------------------------------------------
def _fake_claude(tmp_path: Path, lines: list[str], *, forever: bool = False) -> str:
    """Write a fake `claude` that prints `lines` (one per stdout line). If
    `forever`, it then loops emitting events and never returns (timeout test)."""
    body = ["#!/usr/bin/env bash"]
    for ln in lines:
        body.append(f"printf '%s\\n' {shlex.quote(ln)}")
    if forever:
        evt = json.dumps({"type": "assistant",
                          "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                   "input": {"command": "echo work"}}]}})
        body.append(f"while true; do printf '%s\\n' {shlex.quote(evt)}; sleep 1; done")
    script = tmp_path / "fakeclaude.sh"
    script.write_text("\n".join(body) + "\n")
    script.chmod(0o755)
    return str(script)


def _success_stream() -> list[str]:
    return [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps(_tool_event("Bash", command="openspec new change foo")),
        "a plain non-JSON log line that must be tolerated",
        json.dumps(_tool_event("Write", file_path="/r/src/x.py")),
        json.dumps(_tool_event("Bash", command="python -m pytest -q")),
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "structured_output": {"outcome": "implemented", "summary": "done",
                                          "spec_id": "foo", "pushed": True,
                                          "deployed": True, "tests_passed": True}}),
    ]


def test_streaming_run_parses_result_and_reports_progress(tmp_path: Path):
    fake = _fake_claude(tmp_path, _success_stream())
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"),
                   claude_bin=fake, agent_timeout=30, state_dir=str(tmp_path / "state"))
    snaps: list[Progress] = []
    res = agent.run(cfg, tmp_path, "do x", "", on_progress=snaps.append)

    assert res.outcome == "implemented"
    assert res.spec_id == "foo"
    assert res.pushed and res.deployed and res.tests_passed
    # progress observed, non-decreasing, and it reached at least the tests stage
    assert snaps, "on_progress was never called"
    pcts = [p.pct for p in snaps]
    assert pcts == sorted(pcts)
    assert _STAGE_INDEX[snaps[-1].stage] >= _STAGE_INDEX["tests"]


def test_streaming_run_times_out(tmp_path: Path):
    fake = _fake_claude(tmp_path, [json.dumps({"type": "system", "subtype": "init"})],
                        forever=True)
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"),
                   claude_bin=fake, agent_timeout=2, state_dir=str(tmp_path / "state"))
    res = agent.run(cfg, tmp_path, "do x", "")
    assert res.outcome == "failed"
    assert "timed out" in res.error
    assert res.duration_s < 10        # the hard timeout actually bounded it


def test_timeout_salvages_an_already_emitted_result(tmp_path: Path):
    # The agent emits its terminal `result` event, then a child lingers holding
    # stdout open past the deadline. The run hits the timeout, but the captured
    # result must be salvaged (not recorded as a timeout failure that burns a retry).
    fake = _fake_claude(tmp_path, _success_stream(), forever=True)
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"),
                   claude_bin=fake, agent_timeout=2, state_dir=str(tmp_path / "state"))
    res = agent.run(cfg, tmp_path, "do x", "")
    assert res.outcome == "implemented"      # salvaged, not "failed"/"timed out"
    assert res.pushed and res.deployed and res.tests_passed
    assert res.duration_s < 10               # the hard timeout still bounded the run


def test_missing_claude_binary_fails_cleanly(tmp_path: Path):
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"),
                   claude_bin="/no/such/claude", agent_timeout=5,
                   state_dir=str(tmp_path / "state"))
    res = agent.run(cfg, tmp_path, "do x", "")
    assert res.outcome == "failed" and "not found" in res.error


# --------------------------------------------------------------------------
# Orchestrator write-back: throttle + never-die
# --------------------------------------------------------------------------
def _seed_repo_tab(orch: Orchestrator, repo: Path) -> None:
    be = orch.backend
    be.write_cell("repo", C.CONFIG_ROW, 2, str(repo))
    be.ensure_schema("repo")
    be.write_cell("repo", 4, C.COL_TASK, "do x")


def test_process_task_writes_live_progress_line(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "openspec").mkdir(parents=True)
    fake = _fake_claude(tmp_path, _success_stream())
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"),
                   claude_bin=fake, agent_timeout=30, autonomy="ship",
                   state_dir=str(tmp_path / "state"))
    orch = Orchestrator(cfg)
    _seed_repo_tab(orch, repo)

    writes: list[tuple[int, int, str]] = []
    orig = orch.backend.write_cell

    def spy(title, row, col, value):
        writes.append((row, col, value))
        return orig(title, row, col, value)

    orch.backend.write_cell = spy  # type: ignore[assignment]

    w = WorkItem(title="repo", repo_path=repo, allow_init=False,
                 task=TaskRow(row=4, task="do x", status="queued"),
                 phase="full", spec_id="", next_tries=1)
    orch._process_task(w)

    tab = orch.backend.read_tab("repo")
    assert tab.rows[0].status == C.ST_DONE
    # a live ⏳ progress line was written to the Log column at least once
    assert any("⏳" in v for (_, c, v) in writes if c == C.COL_LOG)


def test_progress_write_failure_cannot_kill_task(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "openspec").mkdir(parents=True)
    fake = _fake_claude(tmp_path, _success_stream())
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"),
                   claude_bin=fake, agent_timeout=30, autonomy="ship",
                   state_dir=str(tmp_path / "state"))
    orch = Orchestrator(cfg)
    _seed_repo_tab(orch, repo)

    orig = orch.backend.write_cell

    def boom(title, row, col, value):
        if col == C.COL_LOG:                 # every Log write explodes
            raise RuntimeError("sheet on fire")
        return orig(title, row, col, value)

    orch.backend.write_cell = boom  # type: ignore[assignment]

    w = WorkItem(title="repo", repo_path=repo, allow_init=False,
                 task=TaskRow(row=4, task="do x", status="queued"),
                 phase="full", spec_id="", next_tries=1)
    orch._process_task(w)  # must NOT raise

    # Status (C) still persisted to done even though every Log write failed.
    tab = orch.backend.read_tab("repo")
    assert tab.rows[0].status == C.ST_DONE
