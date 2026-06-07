"""Offline tests for Stage 3 — add-existing-repo (`add_repo` control handler).

No Google, no claude. Pure MockBackend + the in-process dispatcher.
Run: pytest -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheet_agent import config as C
from sheet_agent import orchestrator as orch_mod
from sheet_agent.orchestrator import Orchestrator, register_control_handler
from sheet_agent.sheets import MockBackend, sanitize_tab_title


@pytest.fixture(autouse=True)
def clean_registry():
    saved = dict(orch_mod.CONTROL_HANDLERS)
    saved_stop = orch_mod._STOP
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


def _grid(mock_path: Path, title: str) -> list[list[str]]:
    return json.loads(Path(mock_path).read_text())["tabs"][title]["grid"]


def _tabs(mock_path: Path) -> dict:
    return json.loads(Path(mock_path).read_text())["tabs"]


def _seed_control_row(be: MockBackend, row: int, *, cid="", ts="", action="",
                      args="", status="", result="") -> None:
    be.ensure_control_schema()
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_ID, cid)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_TS, ts)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_ACTION, action)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_ARGS, args)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_STATUS, status)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_RESULT, result)


def _cell(grid: list[list[str]], row: int, col: int) -> str:
    r, c = row - 1, col - 1
    if 0 <= r < len(grid) and 0 <= c < len(grid[r]):
        return str(grid[r][c])
    return ""


# --- AC #2: title sanitization (pure unit) ---------------------------------
def test_sanitize_last_segment_strips_illegal():
    assert sanitize_tab_title("/a/b/c:d?") == "cd"
    assert len(sanitize_tab_title("/a/b/c:d?")) <= 100


def test_sanitize_strips_each_illegal_char():
    # : \ / ? * [ ] all removed; the last segment only.
    assert sanitize_tab_title("/x/na:m\\e?*[1]") == "name1"


def test_sanitize_caps_at_100():
    seg = "z" * 200
    out = sanitize_tab_title(f"/root/{seg}")
    assert out == "z" * 100
    assert len(out) == 100


def test_sanitize_trailing_slash_and_fallback():
    assert sanitize_tab_title("/a/b/") == "b"           # trailing slash ignored
    # all-illegal / empty segment -> non-empty fallback (never an empty title)
    assert sanitize_tab_title("/a/:?*") != ""
    assert sanitize_tab_title("///") != ""


# --- AC #2 + #4: handler creates a bound, bootstrapped tab from the path -----
def test_add_repo_creates_bound_bootstrapped_tab(tmp_path: Path):
    mock_path = tmp_path / "m.json"
    orch = _mock_orch(tmp_path)
    be = orch.backend
    register_control_handler("add_repo", orch_mod._h_add_repo)
    _seed_control_row(be, 2, cid="a1", ts="2026-06-06 10:00:00Z",
                      action="add_repo", args='{"path": "/a/b/c:d?"}',
                      status=C.CTL_PENDING)

    orch._process_control()

    assert "cd" in _tabs(mock_path)                # title = sanitized last segment
    assert len("cd") <= 100
    grid = _grid(mock_path, "cd")
    assert _cell(grid, C.CONFIG_ROW, C.COL_REPO_BINDING) == "/a/b/c:d?"   # B1 == path
    assert _cell(grid, C.CONFIG_ROW, 1) == C.CONFIG_LABEL_REPO           # A1 "Репозиторий"
    # Row-2 task headers (no Product Vision row). The repo tab carries NO chat —
    # chat moved to `_chat <repo>`.
    assert grid[C.HEADER_ROW - 1][:len(C.HEADERS)] == C.HEADERS
    # a paired chat tab is created, bound by B1 to the SAME path
    chat_title = f"{C.CHAT_TAB_PREFIX}cd"
    assert chat_title in _tabs(mock_path)
    chat_grid = _grid(mock_path, chat_title)
    assert _cell(chat_grid, C.CONFIG_ROW, C.COL_REPO_BINDING) == "/a/b/c:d?"
    assert _cell(chat_grid, C.CHAT_HEADER_ROW, C.COL_CHAT_Q) == C.CHAT_HEADERS[0]
    # control row marked done
    rows = {r.id: r for r in be.read_control()}
    assert rows["a1"].status == C.CTL_DONE


# --- AC #3: title collision suffixed -2, then -3 ---------------------------
def test_add_repo_collision_suffixes(tmp_path: Path):
    mock_path = tmp_path / "m.json"
    orch = _mock_orch(tmp_path)
    register_control_handler("add_repo", orch_mod._h_add_repo)
    be = orch.backend
    # Pre-existing tab named like the target base (different binding so no idempotency).
    be.create_tab("myrepo")
    be.write_cell("myrepo", C.CONFIG_ROW, C.COL_REPO_BINDING, "/already/here")

    # 1st add_repo for a DIFFERENT path whose last segment is also `myrepo`.
    _seed_control_row(be, 2, cid="a1", ts="2026-06-06 10:00:00Z",
                      action="add_repo", args='{"path": "/x/myrepo"}',
                      status=C.CTL_PENDING)
    orch._process_control()
    assert "myrepo-2" in _tabs(mock_path)

    # 3rd same-base, different path -> -3.
    _seed_control_row(be, 3, cid="a2", ts="2026-06-06 11:00:00Z",
                      action="add_repo", args='{"path": "/y/myrepo"}',
                      status=C.CTL_PENDING)
    orch._process_control()
    assert "myrepo-3" in _tabs(mock_path)


# --- AC #5: idempotency — same path twice is a no-op done -------------------
def test_add_repo_idempotent_noop(tmp_path: Path):
    mock_path = tmp_path / "m.json"
    orch = _mock_orch(tmp_path)
    register_control_handler("add_repo", orch_mod._h_add_repo)
    be = orch.backend

    _seed_control_row(be, 2, cid="a1", ts="2026-06-06 10:00:00Z",
                      action="add_repo", args='{"path": "/p/repo"}',
                      status=C.CTL_PENDING)
    orch._process_control()
    count_after_first = len(_tabs(mock_path))
    assert "repo" in _tabs(mock_path)

    # Second add_repo for the SAME path -> no new tab.
    _seed_control_row(be, 3, cid="a2", ts="2026-06-06 11:00:00Z",
                      action="add_repo", args='{"path": "/p/repo"}',
                      status=C.CTL_PENDING)
    orch._process_control()

    assert len(_tabs(mock_path)) == count_after_first     # tab count unchanged
    rows = {r.id: r for r in be.read_control()}
    assert rows["a2"].status == C.CTL_DONE                 # done, NOT error
    assert "already bound" in rows["a2"].result


# --- AC #6: never writes another tab's human-owned cells -------------------
def test_add_repo_never_touches_other_tabs_human_cells(tmp_path: Path):
    mock_path = tmp_path / "m.json"
    orch = _mock_orch(tmp_path)
    register_control_handler("add_repo", orch_mod._h_add_repo)
    be = orch.backend

    # Pre-existing repo tab with human-owned Task (A4) + binding B1, plus a daemon
    # Updated (D4) value — none of which add_repo on a DIFFERENT tab may touch.
    be.create_tab("other")
    be.ensure_schema("other")
    be.write_cell("other", C.CONFIG_ROW, C.COL_REPO_BINDING, "/other/repo")  # B1
    be.write_cell("other", C.FIRST_TASK_ROW, C.COL_TASK, "human task")       # A4
    be.write_cell("other", C.FIRST_TASK_ROW, C.COL_UPDATED, "2026-05-05")    # D4

    before = _grid(mock_path, "other")
    snap = {
        "B1": _cell(before, C.CONFIG_ROW, C.COL_REPO_BINDING),
        "A4": _cell(before, C.FIRST_TASK_ROW, C.COL_TASK),
        "D4": _cell(before, C.FIRST_TASK_ROW, C.COL_UPDATED),
    }

    _seed_control_row(be, 2, cid="a1", ts="2026-06-06 10:00:00Z",
                      action="add_repo", args='{"path": "/brand/new"}',
                      status=C.CTL_PENDING)
    orch._process_control()

    after = _grid(mock_path, "other")
    assert _cell(after, C.CONFIG_ROW, C.COL_REPO_BINDING) == snap["B1"]
    assert _cell(after, C.FIRST_TASK_ROW, C.COL_TASK) == snap["A4"]
    assert _cell(after, C.FIRST_TASK_ROW, C.COL_UPDATED) == snap["D4"]
    # the NEW tab was created and bound to its own path
    assert _cell(_grid(mock_path, "new"), C.CONFIG_ROW, C.COL_REPO_BINDING) == "/brand/new"


# --- AC #7: create_tab backend parity --------------------------------------
def test_create_tab_parity_mock(tmp_path: Path):
    mock_path = tmp_path / "m.json"
    be = MockBackend(str(mock_path))
    be.create_tab("fresh")
    assert "fresh" in _tabs(mock_path)
    assert _tabs(mock_path)["fresh"]["grid"] == []     # empty on create
    # idempotent: second call adds nothing, leaves the grid untouched.
    be.write_cell("fresh", 1, 1, "data")
    be.create_tab("fresh")
    assert _grid(mock_path, "fresh")[0][0] == "data"   # not wiped


def test_create_tab_exists_on_both_backends():
    # Behavioural parity contract: both backends expose create_tab.
    from sheet_agent.sheets import GoogleBackend
    assert hasattr(GoogleBackend, "create_tab")
    assert hasattr(MockBackend, "create_tab")


# --- daemon-never-dies: missing path -> error, no crash --------------------
def test_add_repo_missing_path_errors(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    register_control_handler("add_repo", orch_mod._h_add_repo)
    be = orch.backend
    _seed_control_row(be, 2, cid="a1", ts="2026-06-06 10:00:00Z",
                      action="add_repo", args="{}", status=C.CTL_PENDING)

    orch._process_control()   # must not raise

    rows = {r.id: r for r in be.read_control()}
    assert rows["a1"].status == C.CTL_ERROR
    assert "needs args.path" in rows["a1"].result
