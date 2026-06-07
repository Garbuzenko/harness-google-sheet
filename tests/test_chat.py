"""Offline tests for the read-only per-repo chat, now on a dedicated PAIRED chat tab
(`_chat <repo>`, cols A/B, matched by the B1 binding). No Google, no claude."""
from __future__ import annotations

import json
from pathlib import Path

from sheet_agent import agent
from sheet_agent import config as C
from sheet_agent.agent import ChatResult, _build_chat_cmd, _chat_prompt, _parse
from sheet_agent.orchestrator import ChatWork, Orchestrator
from sheet_agent.sheets import MockBackend, parse_chat_grid


def _mock_orch(tmp_path: Path, **kw) -> Orchestrator:
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"), **kw)
    return Orchestrator(cfg)


def _tabs(mock_path: Path) -> dict:
    return json.loads(Path(mock_path).read_text())["tabs"]


# -- parsing ---------------------------------------------------------------
def test_parse_chat_grid_reads_binding_input_and_transcript():
    grid = [
        ["REPO_PATH", "/srv/r"],                       # A1 label, B1 binding
        ["ask me?", ""],                               # A2 compose box, B2 pin
        ["Ты (вопрос)", "Агент (ответ)"],              # A3/B3 headers
        ["q1", "a1"],                                  # A4/B4 transcript
        ["q2", "a2"],                                  # A5/B5
    ]
    tab = parse_chat_grid("_chat r", grid)
    assert tab.repo_binding == "/srv/r"
    assert tab.chat_input == "ask me?"
    assert [(t.question, t.answer) for t in tab.chat_turns] == [("q1", "a1"), ("q2", "a2")]
    assert tab.chat_turns[0].row == C.CHAT_FIRST_ROW   # 4


def test_ensure_chat_schema_stamps_label_and_headers(tmp_path: Path):
    be = MockBackend(str(tmp_path / "m.json"))
    be.write_cell("_chat r", C.CONFIG_ROW, C.COL_REPO_BINDING, "/srv/r")
    be.ensure_chat_schema("_chat r")
    grid = _tabs(tmp_path / "m.json")["_chat r"]["grid"]
    assert grid[C.CONFIG_ROW - 1][0] == C.CONFIG_LABEL_REPO
    assert grid[C.CHAT_HEADER_ROW - 1][C.COL_CHAT_Q - 1] == C.CHAT_HEADERS[0]
    assert grid[C.CHAT_HEADER_ROW - 1][C.COL_CHAT_A - 1] == C.CHAT_HEADERS[1]


# -- prompt + command ------------------------------------------------------
def test_chat_prompt_includes_history_and_question():
    p = _chat_prompt("why?", [("q1", "a1")])
    assert "why?" in p and "q1" in p and "a1" in p
    empty = _chat_prompt("hi", [])
    assert "Conversation so far" not in empty


def test_chat_cmd_is_read_only():
    cfg = C.Config(backend="mock")
    cmd = _build_chat_cmd(cfg, "prompt")
    s = " ".join(cmd)
    # read-only is enforced at the tool level, WITHOUT a permission bypass
    assert "--allowedTools" in cmd and "Read,Grep,Glob" in cmd
    assert "--disallowedTools" in cmd
    assert "Edit,Write,MultiEdit,NotebookEdit,Bash" in cmd
    assert "--dangerously-skip-permissions" not in s
    assert "--json-schema" in cmd


def test_chat_uses_cheaper_model_than_tasks():
    # chat runs on chat_model (cheaper), NOT the Opus task model — don't burn the
    # premium budget on read-only conversation.
    cfg = C.Config(backend="mock", model="claude-opus-4-8", chat_model="claude-sonnet-4-6")
    cmd = _build_chat_cmd(cfg, "prompt")
    i = cmd.index("--model")
    assert cmd[i + 1] == "claude-sonnet-4-6"
    assert cfg.model not in cmd  # the Opus task model must not leak into the chat cmd


def test_parse_chat_answer_envelope():
    env = json.dumps({"result": "x", "structured_output": {"answer": "because reasons"}})
    assert _parse(env)["answer"] == "because reasons"


# -- orchestrator dispatch -------------------------------------------------
def test_maybe_chat_consumes_box_and_dispatches(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    submitted: list[ChatWork] = []
    orch._run_chat = lambda w: submitted.append(w)   # type: ignore[assignment]

    be = orch.backend
    be.write_cell("_chat r", C.CONFIG_ROW, C.COL_REPO_BINDING, "/srv/r")
    be.write_cell("_chat r", C.CHAT_INPUT_ROW, C.CHAT_INPUT_COL, "what does repo.py do?")
    tab = be.read_chat_tab("_chat r")

    orch._maybe_chat("_chat r", tab, tmp_path)

    tab2 = be.read_chat_tab("_chat r")
    # A2 consumed -> reset to the visible placeholder (not blank), never re-dispatched.
    assert tab2.chat_input == C.CHAT_INPUT_PLACEHOLDER
    assert tab2.chat_turns[0].question == "what does repo.py do?"   # echoed into A4
    assert tab2.chat_turns[0].answer == C.CHAT_THINKING            # placeholder in B4
    # B2 pinned to the thinking marker
    grid = _tabs(tmp_path / "m.json")["_chat r"]["grid"]
    assert grid[C.CHAT_PINNED_ROW - 1][C.CHAT_PINNED_COL - 1] == C.CHAT_THINKING
    assert len(submitted) == 1
    assert submitted[0].answer_row == C.CHAT_FIRST_ROW and submitted[0].question.startswith("what")


def test_maybe_chat_noop_when_box_empty(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    submitted: list[ChatWork] = []
    orch._run_chat = lambda w: submitted.append(w)   # type: ignore[assignment]
    be = orch.backend
    be.write_cell("_chat r", C.CONFIG_ROW, C.COL_REPO_BINDING, "/srv/r")
    tab = be.read_chat_tab("_chat r")   # bootstraps -> A2 seeded with the placeholder
    orch._maybe_chat("_chat r", tab, tmp_path)
    assert submitted == []


def test_ensure_chat_schema_seeds_compose_box_placeholder(tmp_path: Path):
    # An empty compose box A2 gets the visible placeholder so the human can SEE where
    # to ask; seeding is idempotent and never clobbers a typed question.
    be = MockBackend(str(tmp_path / "m.json"))
    be.write_cell("_chat r", C.CONFIG_ROW, C.COL_REPO_BINDING, "/srv/r")
    be.ensure_chat_schema("_chat r")
    grid = _tabs(tmp_path / "m.json")["_chat r"]["grid"]
    assert grid[C.CHAT_INPUT_ROW - 1][C.CHAT_INPUT_COL - 1] == C.CHAT_INPUT_PLACEHOLDER


def test_ensure_chat_schema_does_not_clobber_typed_question(tmp_path: Path):
    be = MockBackend(str(tmp_path / "m.json"))
    be.write_cell("_chat r", C.CONFIG_ROW, C.COL_REPO_BINDING, "/srv/r")
    be.write_cell("_chat r", C.CHAT_INPUT_ROW, C.CHAT_INPUT_COL, "real question?")
    be.ensure_chat_schema("_chat r")
    grid = _tabs(tmp_path / "m.json")["_chat r"]["grid"]
    assert grid[C.CHAT_INPUT_ROW - 1][C.CHAT_INPUT_COL - 1] == "real question?"


def test_maybe_chat_treats_placeholder_as_empty(tmp_path: Path):
    # The placeholder is the "empty" state — it must NEVER be dispatched as a question.
    orch = _mock_orch(tmp_path)
    submitted: list[ChatWork] = []
    orch._run_chat = lambda w: submitted.append(w)   # type: ignore[assignment]
    be = orch.backend
    be.write_cell("_chat r", C.CONFIG_ROW, C.COL_REPO_BINDING, "/srv/r")
    be.write_cell("_chat r", C.CHAT_INPUT_ROW, C.CHAT_INPUT_COL, C.CHAT_INPUT_PLACEHOLDER)
    tab = be.read_chat_tab("_chat r")
    assert tab.chat_input == C.CHAT_INPUT_PLACEHOLDER
    orch._maybe_chat("_chat r", tab, tmp_path)
    assert submitted == []


def test_run_chat_writes_answer_and_pin(tmp_path: Path, monkeypatch):
    orch = _mock_orch(tmp_path)
    monkeypatch.setattr(agent, "chat",
                        lambda *a, **k: ChatResult(answer="repo.py resolves bindings"))
    w = ChatWork(title="_chat r", repo_path=tmp_path, question="?", history=[],
                 answer_row=C.CHAT_FIRST_ROW)
    orch._run_chat(w)
    grid = _tabs(tmp_path / "m.json")["_chat r"]["grid"]
    assert grid[C.CHAT_FIRST_ROW - 1][C.COL_CHAT_A - 1] == "repo.py resolves bindings"
    assert grid[C.CHAT_PINNED_ROW - 1][C.CHAT_PINNED_COL - 1] == "repo.py resolves bindings"


def test_run_chat_rate_limited_marks_backoff(tmp_path: Path, monkeypatch):
    orch = _mock_orch(tmp_path)
    monkeypatch.setattr(agent, "chat",
                        lambda *a, **k: ChatResult(rate_limited=True, error="429"))
    w = ChatWork(title="_chat r", repo_path=tmp_path, question="?", history=[],
                 answer_row=C.CHAT_FIRST_ROW)
    orch._run_chat(w)
    assert orch._backoff is True
    grid = _tabs(tmp_path / "m.json")["_chat r"]["grid"]
    assert "rate-limited" in grid[C.CHAT_FIRST_ROW - 1][C.COL_CHAT_A - 1]


# -- pairing by binding + migration ----------------------------------------
def test_ensure_chat_pair_creates_bound_tab_and_is_idempotent(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    title = orch._ensure_chat_pair("repo", "/srv/r")
    assert title == f"{C.CHAT_TAB_PREFIX}repo"
    grid = _tabs(tmp_path / "m.json")[title]["grid"]
    assert grid[C.CONFIG_ROW - 1][C.COL_REPO_BINDING - 1] == "/srv/r"   # B1 == binding
    # A fresh orchestrator (empty in-cycle cache) finds the existing pair by binding,
    # NOT by title — no duplicate chat tab is created.
    orch2 = _mock_orch(tmp_path)
    again = orch2._ensure_chat_pair("repo", "/srv/r")
    assert again == title
    chat_tabs = [t for t in _tabs(tmp_path / "m.json") if t.startswith(C.CHAT_TAB_PREFIX)]
    assert chat_tabs == [title]


def test_collect_tab_migrates_existing_repo_to_chat_pair(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    be = orch.backend
    repo = tmp_path / "plainrepo"
    (repo / "openspec").mkdir(parents=True)
    be.write_cell("t", C.CONFIG_ROW, C.COL_REPO_BINDING, str(repo))   # B1 binding
    be.ensure_schema("t")

    orch._collect_tab("t")   # repo tab read -> ensures the paired chat tab

    chat_tabs = [tt for tt in _tabs(tmp_path / "m.json") if tt.startswith(C.CHAT_TAB_PREFIX)]
    assert chat_tabs == [f"{C.CHAT_TAB_PREFIX}t"]
    grid = _tabs(tmp_path / "m.json")[chat_tabs[0]]["grid"]
    assert grid[C.CONFIG_ROW - 1][C.COL_REPO_BINDING - 1] == str(repo)


def test_repo_tab_schema_has_no_chat(tmp_path: Path):
    # The repo task tab must no longer carry any chat scaffold (it moved to _chat <repo>).
    be = MockBackend(str(tmp_path / "m.json"))
    be.write_cell("t", C.CONFIG_ROW, C.COL_REPO_BINDING, "/srv/r")
    be.ensure_schema("t")
    tab = be.read_tab("t")
    assert tab.chat_input == "" and tab.chat_turns == []
