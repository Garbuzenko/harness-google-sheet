"""Offline tests for the `_skills` catalog + the async `run_skill` control action.

No Google, no claude. Run: pytest -q

`run_skill` dispatches a real agent through the background pool; these tests
monkeypatch `agent.run` so nothing shells out, and drain the pool before asserting.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheet_agent import config as C
from sheet_agent import agent as agent_mod
from sheet_agent import orchestrator as orch_mod
from sheet_agent.orchestrator import Orchestrator
from sheet_agent.sheets import MockBackend, Skill, TaskRow, parse_skills_grid


@pytest.fixture(autouse=True)
def reset_stop():
    """A leaked `_STOP=True` from another test would make `_process_control` break out
    of its loop immediately. Snapshot/restore it so these tests run deterministically."""
    saved = orch_mod._STOP
    orch_mod._STOP = False
    try:
        yield
    finally:
        orch_mod._STOP = saved


def _mock_orch(tmp_path: Path, **kw) -> Orchestrator:
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"), **kw)
    return Orchestrator(cfg)


def _skills_grid(mock_path: Path) -> list[list[str]]:
    return json.loads(Path(mock_path).read_text())["tabs"][C.SKILLS_TAB]["grid"]


def _control_grid(mock_path: Path) -> list[list[str]]:
    return json.loads(Path(mock_path).read_text())["tabs"][C.CONTROL_TAB]["grid"]


def _seed_control_row(be: MockBackend, row: int, *, cid="", ts="", action="",
                      args="", status="", result="") -> None:
    be.ensure_control_schema()
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_ID, cid)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_TS, ts)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_ACTION, action)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_ARGS, args)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_STATUS, status)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_RESULT, result)


def _bind_repo_tab(be: MockBackend, title: str, repo_path: Path) -> None:
    """Bootstrap a repo tab and bind B1 to an existing dir so `repo.resolve` succeeds."""
    be.ensure_schema(title)
    be.write_cell(title, C.CONFIG_ROW, C.COL_REPO_BINDING, str(repo_path))


# --- AC: constants ---------------------------------------------------------
def test_skills_constants():
    assert C.SKILLS_TAB == "_skills"
    assert C.SKILLS_TAB.startswith(C.META_PREFIX) is True
    assert C.SKILLS_HEADERS == ["Скилл", "Описание", "Промпт", "Запуск"]
    assert C.skill_trigger("idea") == "/idea"
    assert C.ACTION_RUN_SKILL == "run_skill"
    assert C.ACTION_RUN_SKILL in C.ASYNC_CONTROL_ACTIONS


def test_default_catalog_includes_autopilot():
    names = [s.name for s in C.DEFAULT_SKILLS]
    assert "autopilot" in names
    assert len(C.DEFAULT_SKILLS) >= 5            # broad — the operator prunes
    for s in C.DEFAULT_SKILLS:                    # every seed is complete
        assert s.name and s.description and s.prompt


def test_default_catalog_spans_delivery_families():
    """The seed must be comprehensive: at least one skill per delivery family so the
    operator can pick a useful playbook without authoring one (they prune extras)."""
    names = {s.name for s in C.DEFAULT_SKILLS}
    # one representative name per family the catalog must cover
    representatives = [
        "add-tests",          # testing
        "simplify",           # code quality
        "harden",             # robustness & operability
        "security-pass",      # security
        "perf-pass",          # performance
        "refresh-docs",       # documentation
        "code-review",        # engineering hygiene
        "ux-loop-fix",        # running-product UX
    ]
    missing = [n for n in representatives if n not in names]
    assert not missing, f"catalog missing delivery families: {missing}"


def test_default_prompts_are_self_contained():
    """Slash commands / skills are unavailable under `claude -p`; no seeded prompt may
    tell the agent to invoke one (e.g. '/code-review', '/loop-fix'). A slash command
    appears as a slash that starts a token and is immediately followed by a letter."""
    import re
    slash_cmd = re.compile(r"(?:^|\s)/[a-z]")
    for s in C.DEFAULT_SKILLS:
        assert not slash_cmd.search(s.prompt), f"{s.name} prompt invokes a slash command"


# --- AC: ensure_skills_tab seeds once, read_skills round-trips --------------
def test_ensure_skills_tab_seeds_and_read_round_trips(tmp_path: Path):
    mock_path = tmp_path / "m.json"
    be = MockBackend(str(mock_path))
    be.ensure_skills_tab()
    grid = _skills_grid(mock_path)
    assert grid[0] == C.SKILLS_HEADERS
    skills = be.read_skills()
    assert [s.name for s in skills] == [d.name for d in C.DEFAULT_SKILLS]
    assert "autopilot" in {s.name for s in skills}


def test_ensure_skills_tab_seed_is_idempotent_and_never_clobbers(tmp_path: Path):
    mock_path = tmp_path / "m.json"
    be = MockBackend(str(mock_path))
    be.ensure_skills_tab()
    # Human prunes a row + edits a prompt.
    be.write_cell(C.SKILLS_TAB, C.SKILLS_FIRST_ROW, C.COL_SKILL_PROMPT, "EDITED")
    before = _skills_grid(mock_path)

    be.ensure_skills_tab()                        # second call must not re-seed
    after = _skills_grid(mock_path)
    assert after == before, "seed must not clobber human edits on a second call"
    assert after[1][C.COL_SKILL_PROMPT - 1] == "EDITED"


def test_parse_skills_grid_skips_nameless_rows():
    grid = [
        C.SKILLS_HEADERS,
        ["a", "desc-a", "prompt-a"],
        ["", "orphan desc", "orphan prompt"],   # no name -> skipped
        ["b", "", "prompt-b"],
    ]
    skills = parse_skills_grid(grid)
    assert [s.name for s in skills] == ["a", "b"]
    assert skills[0].prompt == "prompt-a"


# --- AC: run_skill dispatches an agent, reports done into the control row ----
def test_run_skill_dispatches_and_marks_done(tmp_path: Path, monkeypatch):
    mock_path = tmp_path / "m.json"
    orch = _mock_orch(tmp_path)
    be = orch.backend
    be.ensure_skills_tab()
    _bind_repo_tab(be, "myrepo", tmp_path)

    seen: dict = {}

    def fake_run(cfg, repo_path, task, detail, **kw):
        seen.update(repo_path=repo_path, task=task, detail=detail)
        return agent_mod.AgentResult(outcome="implemented", spec_id="chg-1",
                                     summary="shipped it")

    monkeypatch.setattr(agent_mod, "run", fake_run)

    _seed_control_row(be, 2, cid="s-1", ts="2026-06-06 10:00:00Z",
                      action=C.ACTION_RUN_SKILL,
                      args=json.dumps({"skill": "autopilot", "tab": "myrepo",
                                       "detail": "add a widget"}),
                      status=C.CTL_PENDING)
    before_ad = _control_grid(mock_path)[1][:4]

    orch._process_control()
    orch._pool.shutdown(wait=True)               # drain the async agent

    # The agent ran against the bound repo with the skill's prompt + the human detail.
    assert seen["repo_path"] == tmp_path
    assert seen["detail"] == "add a widget"
    assert seen["task"]                           # the skill's prompt was passed
    rows = {r.id: r for r in be.read_control()}
    assert rows["s-1"].status == C.CTL_DONE
    assert "autopilot" in rows["s-1"].result and "chg-1" in rows["s-1"].result
    # A-D (Apps-Script-owned) left byte-for-byte unchanged.
    assert _control_grid(mock_path)[1][:4] == before_ad


def test_run_skill_unknown_skill_marks_error(tmp_path: Path, monkeypatch):
    orch = _mock_orch(tmp_path)
    be = orch.backend
    _bind_repo_tab(be, "myrepo", tmp_path)
    monkeypatch.setattr(agent_mod, "run",
                        lambda *a, **k: pytest.fail("agent must not run for unknown skill"))
    _seed_control_row(be, 2, cid="s-2", ts="2026-06-06 10:00:00Z",
                      action=C.ACTION_RUN_SKILL,
                      args=json.dumps({"skill": "does-not-exist", "tab": "myrepo"}),
                      status=C.CTL_PENDING)
    orch._process_control()
    orch._pool.shutdown(wait=True)
    rows = {r.id: r for r in be.read_control()}
    assert rows["s-2"].status == C.CTL_ERROR
    assert "unknown skill" in rows["s-2"].result


def test_run_skill_on_meta_tab_marks_error(tmp_path: Path, monkeypatch):
    orch = _mock_orch(tmp_path)
    be = orch.backend
    be.ensure_skills_tab()
    monkeypatch.setattr(agent_mod, "run",
                        lambda *a, **k: pytest.fail("agent must not run on a meta tab"))
    _seed_control_row(be, 2, cid="s-3", ts="2026-06-06 10:00:00Z",
                      action=C.ACTION_RUN_SKILL,
                      args=json.dumps({"skill": "autopilot", "tab": "_control"}),
                      status=C.CTL_PENDING)
    orch._process_control()
    orch._pool.shutdown(wait=True)
    rows = {r.id: r for r in be.read_control()}
    assert rows["s-3"].status == C.CTL_ERROR
    assert "meta tab" in rows["s-3"].result


def test_run_skill_defers_when_repo_in_flight(tmp_path: Path, monkeypatch):
    orch = _mock_orch(tmp_path)
    be = orch.backend
    be.ensure_skills_tab()
    _bind_repo_tab(be, "myrepo", tmp_path)
    monkeypatch.setattr(agent_mod, "run",
                        lambda *a, **k: pytest.fail("must not dispatch while repo in flight"))
    # Repo already has an agent in flight (a task batch is running there).
    orch._inflight.add(tmp_path)
    _seed_control_row(be, 2, cid="s-4", ts="2026-06-06 10:00:00Z",
                      action=C.ACTION_RUN_SKILL,
                      args=json.dumps({"skill": "autopilot", "tab": "myrepo"}),
                      status=C.CTL_PENDING)
    orch._process_control()
    orch._pool.shutdown(wait=True)
    rows = {r.id: r for r in be.read_control()}
    assert rows["s-4"].status == C.CTL_PENDING, "an in-flight repo must defer the run"


# --- AC: the running daemon self-seeds the catalog -------------------------
def test_run_once_seeds_skills_catalog(tmp_path: Path):
    """A daemon-only operator runs `sheet_agent run` (→ run_once) and must get the
    `_skills` catalog without a separate bootstrap CLI step."""
    mock_path = tmp_path / "m.json"
    orch = _mock_orch(tmp_path)
    # Sanity: no catalog before the first cycle.
    assert C.SKILLS_TAB not in json.loads(mock_path.read_text())["tabs"]

    orch.run_once(drain=True)

    grid = _skills_grid(mock_path)
    assert grid[0] == C.SKILLS_HEADERS
    skills = orch.backend.read_skills()
    assert "autopilot" in {s.name for s in skills}


def test_run_once_survives_skills_seed_failure(tmp_path: Path, monkeypatch):
    """A `_skills` hiccup must never raise out of the cycle (never-die invariant);
    the rest of the cycle (e.g. `_control` bootstrap) must still proceed."""
    orch = _mock_orch(tmp_path)

    def boom():
        raise RuntimeError("skills backend down")

    monkeypatch.setattr(orch.backend, "ensure_skills_tab", boom)
    orch.run_once(drain=True)  # must not raise
    # The cycle carried on past the skills failure: `_control` was still ensured.
    assert C.CONTROL_TAB in json.loads((tmp_path / "m.json").read_text())["tabs"]


def test_run_skill_agent_failure_marks_error(tmp_path: Path, monkeypatch):
    orch = _mock_orch(tmp_path)
    be = orch.backend
    be.ensure_skills_tab()
    _bind_repo_tab(be, "myrepo", tmp_path)
    monkeypatch.setattr(agent_mod, "run",
                        lambda *a, **k: agent_mod.AgentResult(outcome="failed",
                                                              error="boom"))
    _seed_control_row(be, 2, cid="s-5", ts="2026-06-06 10:00:00Z",
                      action=C.ACTION_RUN_SKILL,
                      args=json.dumps({"skill": "autopilot", "tab": "myrepo"}),
                      status=C.CTL_PENDING)
    orch._process_control()
    orch._pool.shutdown(wait=True)
    rows = {r.id: r for r in be.read_control()}
    assert rows["s-5"].status == C.CTL_ERROR
    assert "boom" in rows["s-5"].result


# --- AC: the catalog shows + the daemon resolves the `/<скилл>` trigger ------
def test_seed_exposes_run_trigger_column(tmp_path: Path):
    """A freshly seeded catalog has the Russian header and a `Запуск` column whose
    value is the exact `/<скилл>` trigger the human types into a task cell."""
    mock_path = tmp_path / "m.json"
    be = MockBackend(str(mock_path))
    be.ensure_skills_tab()
    grid = _skills_grid(mock_path)
    assert grid[0] == C.SKILLS_HEADERS                       # Скилл/Описание/Промпт/Запуск
    name = grid[1][C.COL_SKILL_NAME - 1]
    assert grid[1][C.COL_SKILL_RUN - 1] == C.skill_trigger(name)


def test_skills_layout_migration_russianises_and_fills_run_without_clobber(tmp_path: Path):
    """An already-seeded ENGLISH catalog with a hand-edited prompt is migrated: header
    Russianised, `Запуск` filled, curated prompt untouched, idempotent."""
    mock_path = tmp_path / "m.json"
    be = MockBackend(str(mock_path))
    # Old 3-column English catalog, prompt hand-edited.
    be.write_cell(C.SKILLS_TAB, 1, 1, "Skill")
    be.write_cell(C.SKILLS_TAB, 1, 2, "Description")
    be.write_cell(C.SKILLS_TAB, 1, 3, "Prompt")
    be.write_cell(C.SKILLS_TAB, 2, 1, "idea")
    be.write_cell(C.SKILLS_TAB, 2, 2, "desc")
    be.write_cell(C.SKILLS_TAB, 2, 3, "CURATED PROMPT")

    be.ensure_skills_tab()                                   # initialized -> migrate, never re-seed
    grid = _skills_grid(mock_path)
    assert grid[0] == C.SKILLS_HEADERS                       # header Russianised
    assert grid[1][C.COL_SKILL_PROMPT - 1] == "CURATED PROMPT"   # curation untouched
    assert grid[1][C.COL_SKILL_RUN - 1] == "/idea"          # trigger filled

    be.ensure_skills_tab()                                   # idempotent
    assert _skills_grid(mock_path) == grid


def test_resolve_skill_task_plain_and_slash_and_unknown(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    orch._skill_map_cache = {"idea": Skill(name="idea", description="d", prompt="PROMPT-X")}
    # Plain task: passes through unchanged, no detail, no error.
    p, d, err = orch._resolve_skill_task(TaskRow(row=3, task="do a thing", status="queued"))
    assert (p, d, err) == ("do a thing", "", None)
    # Slash trigger: prompt swapped to the skill's prompt, trailing text -> detail.
    p, d, err = orch._resolve_skill_task(TaskRow(row=3, task="/idea focus on auth", status="queued"))
    assert err is None and p == "PROMPT-X" and d == "focus on auth"
    # Unknown skill: error pointing at _skills, no prompt.
    p, d, err = orch._resolve_skill_task(TaskRow(row=3, task="/nope", status="queued"))
    assert p == "" and err is not None and C.SKILLS_TAB in err
    # Bare slash: also an error (no skill named).
    _, _, err2 = orch._resolve_skill_task(TaskRow(row=3, task="/", status="queued"))
    assert err2 is not None


def test_collect_tab_runs_seeded_skill_and_fails_unknown(tmp_path: Path):
    """End-to-end through the collect path on an OpenSpec repo: a `/<скилл>` row yields a
    WorkItem running the catalog skill's prompt (+detail); an unknown `/skill` fails the
    row with a pointer to `_skills` and dispatches nothing."""
    repo = tmp_path / "repo"
    (repo / "openspec").mkdir(parents=True)
    orch = _mock_orch(tmp_path)
    be = orch.backend
    be.ensure_skills_tab()
    _bind_repo_tab(be, "myrepo", repo)

    # A seeded skill trigger -> a WorkItem carrying the skill's prompt + detail.
    be.write_cell("myrepo", C.FIRST_TASK_ROW, C.COL_TASK, "/autopilot extra ctx")
    items = orch._collect_tab("myrepo")
    assert len(items) == 1
    autopilot = {s.name: s for s in be.read_skills()}["autopilot"]
    assert items[0].prompt == autopilot.prompt
    assert items[0].detail == "extra ctx"

    # An unknown trigger -> the row is failed, nothing dispatched.
    orch._skill_map_cache = None                             # new cycle re-reads the catalog
    be.write_cell("myrepo", C.FIRST_TASK_ROW, C.COL_TASK, "/does-not-exist")
    items = orch._collect_tab("myrepo")
    assert items == []
    row = be.read_tab("myrepo").rows[0]
    assert row.status == C.ST_FAILED and C.SKILLS_TAB in row.logmsg


# --------------------------------------------------------------------------
# Operator-invoked catalog top-up (sync_skills)
# --------------------------------------------------------------------------
def _seed_partial_catalog(be: MockBackend, names: list[str],
                          edits: dict[str, str] | None = None) -> None:
    """Seed an already-initialised `_skills` tab (header + the given skill names),
    simulating a catalog seeded before more defaults were added. `edits` maps a
    skill name to a curated prompt so we can prove sync never clobbers it."""
    edits = edits or {}
    for i, h in enumerate(C.SKILLS_HEADERS, start=1):
        be.write_cell(C.SKILLS_TAB, C.SKILLS_HEADER_ROW, i, h)
    for r, n in enumerate(names, start=C.SKILLS_FIRST_ROW):
        be.write_cell(C.SKILLS_TAB, r, C.COL_SKILL_NAME, n)
        be.write_cell(C.SKILLS_TAB, r, C.COL_SKILL_PROMPT, edits.get(n, f"prompt-{n}"))
        be.write_cell(C.SKILLS_TAB, r, C.COL_SKILL_RUN, C.skill_trigger(n))


def test_sync_skills_adds_only_missing_and_keeps_edits(tmp_path: Path):
    be = MockBackend(str(tmp_path / "m.json"))
    # An old catalog: only autopilot (with a curated prompt) — pagespeed is missing.
    _seed_partial_catalog(be, ["autopilot"], edits={"autopilot": "MY CURATED PROMPT"})
    before = {s.name for s in be.read_skills()}
    assert "pagespeed" not in before                  # the reported-bug skill is absent

    added = be.sync_skills()

    assert "pagespeed" in added                        # the missing default was added
    assert "autopilot" not in added                    # already present → not re-added
    after = {s.name: s for s in be.read_skills()}
    assert "pagespeed" in after
    # Every default skill is now present (so /<skill> resolves for all of them).
    assert {s.name for s in C.DEFAULT_SKILLS} <= set(after)
    # The curated prompt is left exactly as the operator wrote it.
    assert after["autopilot"].prompt == "MY CURATED PROMPT"


def test_sync_skills_noop_when_complete(tmp_path: Path):
    be = MockBackend(str(tmp_path / "m.json"))
    be.ensure_skills_tab()                              # full default seed
    assert be.sync_skills() == []                       # nothing missing → no-op


def test_skills_cli_sync_tops_up(tmp_path: Path):
    from sheet_agent.__main__ import _skills
    path = str(tmp_path / "m.json")
    be = MockBackend(path)
    _seed_partial_catalog(be, ["autopilot"])
    rc = _skills(C.Config(backend="mock", mock_path=path), sync=True)
    assert rc == 0
    names = {s.name for s in MockBackend(path).read_skills()}
    assert {s.name for s in C.DEFAULT_SKILLS} <= names
