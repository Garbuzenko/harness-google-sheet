"""Offline tests for the stall watchdog (Layer 2) and bash tool-call bounds (Layer 1).

A hang's real shape is a SILENT output stream — an unbounded `until … grep` bash, or
a child that holds the stdout pipe open so `claude` itself blocks — not a process that
keeps emitting events until the coarse wall-clock `AGENT_TIMEOUT`. These tests use a
fake `CLAUDE_BIN` that goes silent on purpose; no Google, no real claude.
"""
from __future__ import annotations

import json
import shlex
import time
from pathlib import Path

from sheet_agent import config as C
from sheet_agent import agent
from sheet_agent.agent import _bash_bound_env


def _result_line(**fields: object) -> str:
    payload = {"outcome": "implemented", "summary": "done"}
    payload.update(fields)
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "structured_output": payload})


def _fake_claude(tmp_path: Path, *, pre_lines: list[str], sleep_s: int) -> str:
    """A fake `claude` that prints each `pre_lines` line, then sleeps `sleep_s`
    seconds in silence (mimicking a wedged tool call / hung child)."""
    body = ["#!/usr/bin/env bash", "set -e"]
    for ln in pre_lines:
        body.append(f"printf '%s\\n' {shlex.quote(ln)}")
    body.append(f"sleep {sleep_s}")
    script = tmp_path / "fakeclaude.sh"
    script.write_text("\n".join(body) + "\n")
    script.chmod(0o755)
    return str(script)


def _cfg(tmp_path: Path, fake: str, **over) -> C.Config:
    base = dict(backend="mock", mock_path=str(tmp_path / "m.json"),
                claude_bin=fake, agent_timeout=300, agent_stall_timeout=1,
                chat_timeout=300, chat_stall_timeout=1,
                state_dir=str(tmp_path / "state"))
    base.update(over)
    return C.Config(**base)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "openspec" / "changes").mkdir(parents=True)
    return tmp_path


# --------------------------------------------------------------------------
# Layer 1: bash tool-call bounds are injected into the child env
# --------------------------------------------------------------------------
def test_bash_bound_env_carries_the_defaults(tmp_path: Path):
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"))
    env = _bash_bound_env(cfg)
    assert env["BASH_DEFAULT_TIMEOUT_MS"] == "120000"
    assert env["BASH_MAX_TIMEOUT_MS"] == "900000"
    assert "PATH" in env  # inherits the real environment too


def test_bash_bound_env_respects_overrides(tmp_path: Path):
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"),
                   bash_max_timeout_ms=300000)
    assert _bash_bound_env(cfg)["BASH_MAX_TIMEOUT_MS"] == "300000"


# --------------------------------------------------------------------------
# Layer 2: stall watchdog kills a silent agent before the wall-clock timeout
# --------------------------------------------------------------------------
def test_run_kills_silent_agent_as_stall(tmp_path: Path):
    repo = _repo(tmp_path)
    # Emits one non-result line, then 30s of silence — but agent_stall_timeout=1.
    fake = _fake_claude(repo, pre_lines=["{\"type\": \"system\"}"], sleep_s=30)
    t0 = time.monotonic()
    res = agent.run(_cfg(tmp_path, fake), repo, "do x", "")
    dur = time.monotonic() - t0
    assert res.outcome == "failed"
    assert "stall" in (res.error or "").lower()
    # Killed on the stall window (~1s), nowhere near the 300s wall-clock timeout.
    assert dur < 15


def test_run_salvages_emitted_result_even_when_it_then_stalls(tmp_path: Path):
    repo = _repo(tmp_path)
    # Emits a terminal result, then lingers silent (a child holding the pipe open).
    fake = _fake_claude(repo, pre_lines=[_result_line()], sleep_s=30)
    res = agent.run(_cfg(tmp_path, fake), repo, "do x", "")
    assert res.outcome == "implemented"   # salvaged, NOT a stall failure


def test_chat_kills_silent_agent_as_stall(tmp_path: Path):
    repo = _repo(tmp_path)
    fake = _fake_claude(repo, pre_lines=["{\"type\": \"system\"}"], sleep_s=30)
    res = agent.chat(_cfg(tmp_path, fake), repo, "what is x?")
    assert res.error and "stall" in res.error.lower()
