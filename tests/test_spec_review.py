"""Offline tests for surfacing a gated spec for review: the daemon attaches the
proposal + spec deltas as a NOTE on the Спека cell when a spec is ready, so the
human can read it before approving.

Run: pytest -q -k spec_review
"""
from __future__ import annotations

from pathlib import Path

from sheet_agent import agent
from sheet_agent import config as C
from sheet_agent.orchestrator import Orchestrator, SheetCtx, WorkItem


def _mock_orch(tmp_path: Path, **kw) -> Orchestrator:
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"), **kw)
    return Orchestrator(cfg)


def _change(repo: Path, spec_id: str, proposal: str, delta: str | None = None) -> None:
    base = repo / "openspec" / "changes" / spec_id
    (base / "specs" / "cap").mkdir(parents=True, exist_ok=True)
    (base / "proposal.md").write_text(proposal, encoding="utf-8")
    if delta is not None:
        (base / "specs" / "cap" / "spec.md").write_text(delta, encoding="utf-8")


# --- _spec_digest reads proposal + deltas -----------------------------------
def test_spec_digest_includes_proposal_and_deltas(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    repo = tmp_path / "r"
    _change(repo, "add-x", "# Why\nbecause\n## What Changes\n- a thing",
            "### Requirement: X\nit shall do X")
    digest = orch._spec_digest(repo, "add-x")
    assert "What Changes" in digest
    assert "Requirement: X" in digest
    assert "spec.md" in digest          # delta file is labelled


def test_spec_digest_missing_returns_empty(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    assert orch._spec_digest(tmp_path / "nope", "ghost") == ""
    assert orch._spec_digest(tmp_path / "r", "") == ""


def test_spec_digest_capped(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    repo = tmp_path / "r"
    _change(repo, "big", "x" * (C.COL_SPEC_NOTE_MAX + 5000))
    assert len(orch._spec_digest(repo, "big")) == C.COL_SPEC_NOTE_MAX


# --- spec_ready attaches the note on the Спека cell -------------------------
def test_spec_ready_attaches_review_note(tmp_path: Path, monkeypatch):
    orch = _mock_orch(tmp_path, autonomy="gated")
    be = orch.backend
    repo = tmp_path / "r"
    _change(repo, "add-x", "# Why\nbecause\n## What Changes\n- a thing",
            "### Requirement: X\nit shall do X")
    be.write_cell("t", C.CONFIG_ROW, C.COL_REPO_BINDING, str(repo))
    be.ensure_schema("t")
    be.write_cell("t", C.FIRST_TASK_ROW, C.COL_TASK, "do x")
    be.write_cell("t", C.FIRST_TASK_ROW, C.COL_STATUS, C.ST_QUEUED)

    ctx = SheetCtx(backend=be, autonomy="gated", label="master")
    t = be.read_tab("t").rows[0]
    w = WorkItem(title="t", repo_path=repo, allow_init=False, task=t, phase="spec",
                 spec_id="", next_tries=1, prompt="do x", ctx=ctx)
    monkeypatch.setattr(agent, "run", lambda *a, **k: agent.AgentResult(
        outcome="spec_ready", spec_id="add-x", summary="спека готова"))

    orch._process_task(w)

    row = be.read_tab("t").rows[0]
    assert row.status == C.ST_SPEC_READY
    assert row.spec == "add-x"                          # change id still in column B
    note = be.get_note("t", C.FIRST_TASK_ROW, C.COL_SPEC)
    assert "What Changes" in note                       # full proposal in the cell note
    assert "Requirement: X" in note                     # and the spec delta
    assert "примечание" in row.logmsg.lower()           # Log points the human to it


def test_spec_ready_without_files_falls_back(tmp_path: Path, monkeypatch):
    """No spec files on disk → no note, and the id-only message is used (no crash)."""
    orch = _mock_orch(tmp_path, autonomy="gated")
    be = orch.backend
    repo = tmp_path / "r"
    (repo / "openspec").mkdir(parents=True)
    be.write_cell("t", C.CONFIG_ROW, C.COL_REPO_BINDING, str(repo))
    be.ensure_schema("t")
    be.write_cell("t", C.FIRST_TASK_ROW, C.COL_TASK, "do x")
    be.write_cell("t", C.FIRST_TASK_ROW, C.COL_STATUS, C.ST_QUEUED)

    ctx = SheetCtx(backend=be, autonomy="gated", label="master")
    t = be.read_tab("t").rows[0]
    w = WorkItem(title="t", repo_path=repo, allow_init=False, task=t, phase="spec",
                 spec_id="", next_tries=1, prompt="do x", ctx=ctx)
    monkeypatch.setattr(agent, "run", lambda *a, **k: agent.AgentResult(
        outcome="spec_ready", spec_id="ghost", summary="спека"))

    orch._process_task(w)

    assert be.read_tab("t").rows[0].status == C.ST_SPEC_READY
    assert be.get_note("t", C.FIRST_TASK_ROW, C.COL_SPEC) == ""   # nothing to attach
