"""Offline tests for Stage 2 of friend sheets: the live multi-sheet poll loop and
its enforcement — per-sheet backend routing, allowlist rejection, per-sheet
autonomy, the friend `_control` guard, and chat on friend sheets.

No Google, no claude: friend sheets are sibling MockBackend JSON files and the
agent is stubbed. Run: pytest -q -k friend_sheets_loop
"""
from __future__ import annotations

from pathlib import Path

from sheet_agent import agent
from sheet_agent import config as C
from sheet_agent import sheets
from sheet_agent.orchestrator import Orchestrator, SheetCtx
from sheet_agent.sheets import MockBackend


def _mock_orch(tmp_path: Path, **kw) -> Orchestrator:
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"), **kw)
    return Orchestrator(cfg)


def _repo(tmp_path: Path, name: str, *, openspec: bool = True) -> Path:
    p = tmp_path / name
    p.mkdir(parents=True, exist_ok=True)
    if openspec:
        (p / "openspec").mkdir(exist_ok=True)
    return p


def _friend_be(tmp_path: Path, sid: str) -> MockBackend:
    """The MockBackend `make_friend_backend` resolves to for `sid` (sibling file)."""
    return MockBackend(str(tmp_path / f"friend-{sid}.json"))


def _bind_repo_tab(be: MockBackend, title: str, binding: str,
                   *, task: str = "do x", status: str = C.ST_QUEUED) -> None:
    be.write_cell(title, C.CONFIG_ROW, C.COL_REPO_BINDING, binding)  # B1
    be.ensure_schema(title)
    if task:
        be.write_cell(title, C.FIRST_TASK_ROW, C.COL_TASK, task)
        be.write_cell(title, C.FIRST_TASK_ROW, C.COL_STATUS, status)


def _friend_ctx(orch: Orchestrator, be: MockBackend, binding: str,
                autonomy: str, sid: str = "FID") -> SheetCtx:
    return SheetCtx(backend=be, autonomy=autonomy, label=sid[:8],
                    friend=sheets.Friend(sheet_id=sid, repos=[binding],
                                         autonomy=autonomy))


# --- AC 7.1: per-sheet backend factory + write-back routing -----------------
def test_make_friend_backend_mock_is_sibling_file(tmp_path: Path):
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"))
    be = sheets.make_friend_backend(cfg, "FID")
    be.write_cell("x", 1, 1, "v")
    assert (tmp_path / "friend-FID.json").exists()


def test_set_routes_writeback_to_ctx_backend(tmp_path: Path):
    """A friend-sheet write-back lands on the friend backend, never the master."""
    orch = _mock_orch(tmp_path)
    fbe = _friend_be(tmp_path, "FID")
    ctx = _friend_ctx(orch, fbe, "repo-a", "gated")
    _bind_repo_tab(fbe, "t", "repo-a")

    orch._set("t", C.FIRST_TASK_ROW, status=C.ST_WORKING, ctx=ctx)

    # The status is on the FRIEND sheet…
    assert fbe.read_tab("t").rows[0].status == C.ST_WORKING
    # …and the master sheet never grew a tab `t`.
    assert "t" not in orch.backend.list_tab_titles()


def test_friend_contexts_built_from_registry(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    orch.backend.append_friend("FID1", ["repo-a"], "p@x.com", "gated", "u1")
    orch.backend.append_friend("FID2", ["repo-b"], "q@x.com", "ship", "u2")

    ctxs = orch._friend_contexts()

    assert [c.label for c in ctxs] == ["FID1", "FID2"]
    assert [c.autonomy for c in ctxs] == ["gated", "ship"]
    assert all(c.is_friend for c in ctxs)
    # Each ctx carries a DISTINCT backend pinned to its own sheet id, never the master.
    assert ctxs[0].backend is not ctxs[1].backend
    assert ctxs[0].backend is not orch.backend
    assert ctxs[1].backend is not orch.backend


# --- AC 7.3: allowlist enforcement ------------------------------------------
def test_friend_allowlist_admits_shared_repo(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    repo = _repo(tmp_path, "shared")
    fbe = _friend_be(tmp_path, "FID")
    ctx = _friend_ctx(orch, fbe, str(repo), "ship")
    _bind_repo_tab(fbe, "shared", str(repo))

    items = orch._collect_tab("shared", ctx)

    assert len(items) == 1
    assert items[0].ctx is ctx
    assert items[0].repo_path == repo


def test_friend_allowlist_blocks_out_of_scope_repo(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    allowed = _repo(tmp_path, "allowed")
    other = _repo(tmp_path, "other")
    fbe = _friend_be(tmp_path, "FID")
    # allowlist = {allowed}; the tab is bound to `other` (out of scope).
    ctx = _friend_ctx(orch, fbe, str(allowed), "ship")
    _bind_repo_tab(fbe, "other", str(other))

    items = orch._collect_tab("other", ctx)

    assert items == []
    row = fbe.read_tab("other").rows[0]
    assert row.status == C.ST_BLOCKED
    assert "allowlist" in row.logmsg.lower() or "shared" in row.logmsg.lower()


# --- AC 7.4: per-sheet autonomy ---------------------------------------------
def test_friend_autonomy_gated_plans_spec_phase(tmp_path: Path):
    # Master is `ship`, but the friend file is `gated` → its task must run SPEC only.
    orch = _mock_orch(tmp_path, autonomy="ship")
    repo = _repo(tmp_path, "r")
    fbe = _friend_be(tmp_path, "FID")
    ctx = _friend_ctx(orch, fbe, str(repo), "gated")
    _bind_repo_tab(fbe, "r", str(repo))

    items = orch._collect_tab("r", ctx)

    assert len(items) == 1
    assert items[0].phase == "spec"


def test_friend_autonomy_ship_plans_full_phase(tmp_path: Path):
    orch = _mock_orch(tmp_path, autonomy="gated")   # master gated, friend ship
    repo = _repo(tmp_path, "r")
    fbe = _friend_be(tmp_path, "FID")
    ctx = _friend_ctx(orch, fbe, str(repo), "ship")
    _bind_repo_tab(fbe, "r", str(repo))

    items = orch._collect_tab("r", ctx)

    assert len(items) == 1
    assert items[0].phase == "full"


# --- AC 7.5: friend `_control` guard ----------------------------------------
def _seed_ctl(be: MockBackend, row: int, action: str) -> None:
    be.ensure_control_schema()
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_ID, f"id{row}")
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_ACTION, action)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_ARGS, "{}")
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_STATUS, C.CTL_PENDING)


def test_friend_control_rejects_repo_creation(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    fbe = _friend_be(tmp_path, "FID")
    ctx = _friend_ctx(orch, fbe, "repo-a", "gated")
    _seed_ctl(fbe, 2, "add_repo")
    _seed_ctl(fbe, 3, "create_repo")
    _seed_ctl(fbe, 4, "run_skill")   # not a repo-creating action → left alone

    orch._guard_friend_control(ctx)

    rows = {cr.action: cr for cr in fbe.read_control()}
    assert rows["add_repo"].status == C.CTL_ERROR
    assert rows["create_repo"].status == C.CTL_ERROR
    assert "not permitted" in rows["add_repo"].result.lower()
    # A non-creating action is ignored (never dispatched), left pending.
    assert rows["run_skill"].status == C.CTL_PENDING


def test_friend_control_guard_only_runs_when_tab_exists(tmp_path: Path):
    """`_poll_sheet` must not bootstrap a `_control` tab on a friend file (that would
    hand the partner a control surface). A friend sheet with no `_control` gets none."""
    orch = _mock_orch(tmp_path)
    repo = _repo(tmp_path, "r")
    fbe = _friend_be(tmp_path, "FID")
    ctx = _friend_ctx(orch, fbe, str(repo), "ship")
    _bind_repo_tab(fbe, "r", str(repo))

    orch._poll_sheet(ctx)

    assert C.CONTROL_TAB not in fbe.list_tab_titles()


# --- AC: chat on friend sheets ----------------------------------------------
def test_ensure_chat_pair_creates_on_friend_sheet(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    fbe = _friend_be(tmp_path, "FID")
    ctx = _friend_ctx(orch, fbe, "repo-a", "gated")
    orch._chat_pairs = {}

    title = orch._ensure_chat_pair("repo-a", "repo-a", ctx)

    assert title is not None and title.startswith(C.CHAT_TAB_PREFIX)
    # The chat tab lives on the FRIEND sheet, not the master.
    assert title in fbe.list_tab_titles()
    assert title not in orch.backend.list_tab_titles()


def test_friend_chat_question_answered_on_friend_sheet(tmp_path: Path, monkeypatch):
    orch = _mock_orch(tmp_path)
    repo = _repo(tmp_path, "r")
    fbe = _friend_be(tmp_path, "FID")
    ctx = _friend_ctx(orch, fbe, str(repo), "gated")
    # A chat tab on the friend sheet with a pending question in the compose box.
    chat_title = f"{C.CHAT_TAB_PREFIX}r"
    fbe.create_tab(chat_title)
    fbe.write_cell(chat_title, C.CONFIG_ROW, C.COL_REPO_BINDING, str(repo))
    fbe.ensure_chat_schema(chat_title)
    fbe.write_cell(chat_title, C.CHAT_INPUT_ROW, C.CHAT_INPUT_COL, "how big?")

    monkeypatch.setattr(agent, "chat",
                        lambda cfg, path, q, hist: agent.ChatResult(answer="42 files"))
    orch._chat_pairs = {}

    ct = fbe.read_chat_tab(chat_title)
    orch._run_chat_tab(chat_title, ct, ctx)
    orch._chat_pool.shutdown(wait=True)   # drain the async chat turn

    after = fbe.read_chat_tab(chat_title)
    # The compose box was consumed (reset to the placeholder) and the answer recorded
    # in the transcript — all on the FRIEND sheet.
    assert after.chat_input == C.CHAT_INPUT_PLACEHOLDER
    assert any("42 files" in t.answer for t in after.chat_turns)
    # The master never grew a chat tab.
    assert chat_title not in orch.backend.list_tab_titles()


# --- AC 7.2: end-to-end multi-sheet cycle -----------------------------------
def test_run_once_polls_master_and_friend(tmp_path: Path, monkeypatch):
    orch = _mock_orch(tmp_path, autonomy="ship")
    master_repo = _repo(tmp_path, "master-repo")
    friend_repo = _repo(tmp_path, "friend-repo")

    # Master task tab.
    _bind_repo_tab(orch.backend, "mtab", str(master_repo))
    # Friend sheet FID with its own task tab, registered + allowlisted (ship).
    fbe = _friend_be(tmp_path, "FID")
    _bind_repo_tab(fbe, "ftab", str(friend_repo))
    orch.backend.append_friend("FID", [str(friend_repo)], "p@x.com", "ship", "u")

    seen: list[Path] = []

    def _fake_run(cfg, path, prompt, detail, **kw):
        seen.append(path)
        return agent.AgentResult(outcome="implemented", summary="готово",
                                 pushed=True, deployed=True)

    monkeypatch.setattr(agent, "run", _fake_run)

    orch.run_once(drain=True)

    # Both repos were dispatched, each from its own sheet.
    assert set(seen) == {master_repo, friend_repo}
    # Status written back to the ORIGINATING sheet, never crossed.
    assert orch.backend.read_tab("mtab").rows[0].status == C.ST_DONE
    assert fbe.read_tab("ftab").rows[0].status == C.ST_DONE
    # The friend's task tab never appeared on the master sheet.
    assert "ftab" not in orch.backend.list_tab_titles()
