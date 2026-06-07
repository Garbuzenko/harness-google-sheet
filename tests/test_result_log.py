"""Offline tests for the Russian-caveman result log (column F).

The log a human reads after a run — the agent's `summary`/`error` and the
daemon's own outcome notes — must be Russian, caveman register, без воды. No
Google, no real claude here: the agent is monkeypatched.
"""
from __future__ import annotations

from pathlib import Path

from sheet_agent import config as C
from sheet_agent import agent as agent_mod
from sheet_agent.agent import RESULT_SCHEMA, SYSTEM_PROMPT, AgentResult
from sheet_agent.orchestrator import Orchestrator, WorkItem, SkillRun
from sheet_agent.sheets import MockBackend, TaskRow


def _has_cyrillic(s: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in s)


def _mock_orch(tmp_path: Path, **kw) -> Orchestrator:
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"), **kw)
    return Orchestrator(cfg)


def _seed_repo_tab(orch: Orchestrator, repo: Path) -> None:
    be = orch.backend
    be.write_cell("repo", C.CONFIG_ROW, C.COL_REPO_BINDING, str(repo))
    be.ensure_schema("repo")
    be.write_cell("repo", 4, C.COL_TASK, "do x")


def _row_log(orch: Orchestrator) -> str:
    return orch.backend.read_tab("repo").rows[0].logmsg


# --------------------------------------------------------------------------
# AC: the result schema and system prompt demand Russian caveman без воды
# --------------------------------------------------------------------------
def test_result_schema_demands_russian_caveman():
    summary = RESULT_SCHEMA["properties"]["summary"]["description"]
    error = RESULT_SCHEMA["properties"]["error"]["description"]
    assert _has_cyrillic(summary) and "без воды" in summary
    assert _has_cyrillic(error) and "без воды" in error


def test_system_prompt_demands_russian_for_summary_and_error():
    assert _has_cyrillic(SYSTEM_PROMPT)
    assert "без воды" in SYSTEM_PROMPT
    assert "summary" in SYSTEM_PROMPT and "error" in SYSTEM_PROMPT


# --------------------------------------------------------------------------
# AC: the daemon's terminal task note (column F) is Russian, keeps its tokens
# --------------------------------------------------------------------------
def test_spec_ready_note_is_russian_and_keeps_approved_token(tmp_path: Path, monkeypatch):
    orch = _mock_orch(tmp_path, autonomy="gated")
    repo = tmp_path / "repo"
    (repo / "openspec").mkdir(parents=True)
    _seed_repo_tab(orch, repo)
    monkeypatch.setattr(agent_mod, "run", lambda *a, **k: AgentResult(
        outcome="spec_ready", spec_id="chg-x", summary="я делать спеку"))

    w = WorkItem(title="repo", repo_path=repo, allow_init=False,
                 task=TaskRow(row=4, task="do x", status="queued"),
                 phase="spec", spec_id="", next_tries=1)
    orch._process_task(w)

    note = _row_log(orch)
    assert orch.backend.read_tab("repo").rows[0].status == C.ST_SPEC_READY
    assert _has_cyrillic(note)
    assert "approved" in note          # the actionable token survives translation


def test_failed_fallback_note_is_russian(tmp_path: Path, monkeypatch):
    orch = _mock_orch(tmp_path, autonomy="ship")
    repo = tmp_path / "repo"
    (repo / "openspec").mkdir(parents=True)
    _seed_repo_tab(orch, repo)
    # outcome failed, no summary/error → the daemon's own fallback is used.
    monkeypatch.setattr(agent_mod, "run", lambda *a, **k: AgentResult(outcome="failed"))

    w = WorkItem(title="repo", repo_path=repo, allow_init=False,
                 task=TaskRow(row=4, task="do x", status="queued"),
                 phase="full", spec_id="", next_tries=1)
    orch._process_task(w)

    note = _row_log(orch)
    assert orch.backend.read_tab("repo").rows[0].status == C.ST_FAILED
    assert _has_cyrillic(note) and "failed" not in note


# --------------------------------------------------------------------------
# AC: the skill outcome note is Russian but keeps the skill name and spec id
# --------------------------------------------------------------------------
def test_skill_outcome_note_is_russian_with_machine_tokens(tmp_path: Path, monkeypatch):
    orch = _mock_orch(tmp_path)
    be: MockBackend = orch.backend
    be.ensure_control_schema()
    be.write_cell(C.CONTROL_TAB, 2, C.COL_CTL_ID, "s-1")
    monkeypatch.setattr(agent_mod, "run", lambda *a, **k: AgentResult(
        outcome="implemented", spec_id="chg-1", summary="я делать фичу"))

    sr = SkillRun(control_row=2, skill="autopilot", tab_title="repo",
                  repo_path=tmp_path, task="prompt", detail="")
    orch._run_skill(sr)

    rows = {r.id: r for r in be.read_control()}
    note = rows["s-1"].result
    assert rows["s-1"].status == C.CTL_DONE
    assert _has_cyrillic(note)
    assert "autopilot" in note and "chg-1" in note   # machine tokens preserved
