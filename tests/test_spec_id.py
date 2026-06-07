"""Offline tests for deterministic OpenSpec change-id detection.

The agent's self-reported `spec_id` is optional and routinely omitted, which used
to leave the sheet's Spec column (B) blank. `agent.run` now derives the id from
the filesystem (diffing `openspec/changes/` before/after the run). These tests use
a fake `CLAUDE_BIN` that actually creates a change folder, so the detection is
exercised end-to-end with no Google and no real claude.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from sheet_agent import config as C
from sheet_agent import agent
from sheet_agent.agent import _list_change_ids, _resolve_spec_id, _spec_from_disk


def _result_line(**fields: object) -> str:
    payload = {"outcome": "implemented", "summary": "done"}
    payload.update(fields)
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "structured_output": payload})


def _fake_claude(tmp_path: Path, *, creates: list[str], result_line: str) -> str:
    """A fake `claude` that first creates each `creates` change folder under
    `openspec/changes/` (relative to its cwd = the repo) and then prints a single
    stream-json `result` line. Mimics an agent that authored a change."""
    body = ["#!/usr/bin/env bash", "set -e"]
    for cid in creates:
        body.append(f"mkdir -p {shlex.quote(f'openspec/changes/{cid}')}")
    body.append(f"printf '%s\\n' {shlex.quote(result_line)}")
    script = tmp_path / "fakeclaude.sh"
    script.write_text("\n".join(body) + "\n")
    script.chmod(0o755)
    return str(script)


def _cfg(tmp_path: Path, fake: str) -> C.Config:
    return C.Config(backend="mock", mock_path=str(tmp_path / "m.json"),
                    claude_bin=fake, agent_timeout=30,
                    state_dir=str(tmp_path / "state"))


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "openspec" / "changes").mkdir(parents=True)
    return tmp_path


# --------------------------------------------------------------------------
# pure-unit helpers
# --------------------------------------------------------------------------
def test_list_change_ids_includes_active_and_archived(tmp_path: Path):
    base = tmp_path / "openspec" / "changes"
    (base / "add-foo").mkdir(parents=True)
    (base / "archive" / "2026-01-01-old").mkdir(parents=True)
    ids = _list_change_ids(tmp_path)
    assert ids == {"add-foo", "2026-01-01-old"}   # the `archive` dir itself excluded


def test_list_change_ids_missing_tree_is_empty(tmp_path: Path):
    assert _list_change_ids(tmp_path) == set()    # no openspec/ → empty, not error


def test_resolve_prefers_single_new_folder(tmp_path: Path):
    repo = _repo(tmp_path)
    before = _list_change_ids(repo)
    (repo / "openspec" / "changes" / "add-foo").mkdir()
    # reported is empty, but the lone new folder is authoritative.
    assert _resolve_spec_id(repo, before, "", "full", "") == "add-foo"


def test_resolve_implement_phase_keeps_given(tmp_path: Path):
    repo = _repo(tmp_path)
    before = _list_change_ids(repo)
    assert _resolve_spec_id(repo, before, "", "implement", "approved-x") == "approved-x"


def test_resolve_falls_back_to_report_when_nothing_new(tmp_path: Path):
    repo = _repo(tmp_path)
    before = _list_change_ids(repo)
    assert _resolve_spec_id(repo, before, "edited-existing", "full", "") == "edited-existing"


def test_spec_from_disk_is_empty_when_ambiguous(tmp_path: Path):
    repo = _repo(tmp_path)
    before = _list_change_ids(repo)
    (repo / "openspec" / "changes" / "a").mkdir()
    (repo / "openspec" / "changes" / "b").mkdir()
    assert _spec_from_disk(repo, before) == ""    # two new → ambiguous


# --------------------------------------------------------------------------
# end-to-end via agent.run + a fake claude
# --------------------------------------------------------------------------
def test_run_detects_spec_id_when_agent_omits_it(tmp_path: Path):
    repo = _repo(tmp_path)
    # The agent creates the folder but DOES NOT report spec_id (the real-world bug).
    fake = _fake_claude(repo, creates=["add-foo"], result_line=_result_line())
    res = agent.run(_cfg(tmp_path, fake), repo, "do x", "")
    assert res.outcome == "implemented"
    assert res.spec_id == "add-foo"               # recovered from disk


def test_run_uses_reported_id_when_it_matches_disk(tmp_path: Path):
    repo = _repo(tmp_path)
    fake = _fake_claude(repo, creates=["add-foo"],
                        result_line=_result_line(spec_id="add-foo"))
    res = agent.run(_cfg(tmp_path, fake), repo, "do x", "")
    assert res.spec_id == "add-foo"


def test_run_implement_phase_keeps_approved_id(tmp_path: Path):
    repo = _repo(tmp_path)
    # No new folder; an approved change is being implemented.
    fake = _fake_claude(repo, creates=[], result_line=_result_line())
    res = agent.run(_cfg(tmp_path, fake), repo, "do x", "",
                    phase="implement", spec_id="approved-x")
    assert res.spec_id == "approved-x"


def test_run_no_change_no_report_stays_empty(tmp_path: Path):
    repo = _repo(tmp_path)
    fake = _fake_claude(repo, creates=[], result_line=_result_line())
    res = agent.run(_cfg(tmp_path, fake), repo, "do x", "")
    assert res.spec_id == ""
