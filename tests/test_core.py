"""Offline tests — no Google, no claude. Run: pytest -q"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheet_agent import config as C
from sheet_agent.agent import _parse, _RATE_RE, _task_prompt
from sheet_agent.orchestrator import Orchestrator, SingleInstance
from sheet_agent.repo import resolve, has_openspec, discover, _looks_like_git_url
from sheet_agent.sheets import MockBackend, TaskRow, parse_grid


def test_parse_grid_reads_config_and_rows():
    grid = [
        ["REPO_PATH", "/srv/myrepo", "BRANCH", "main"],
        ["Task", "Spec", "Status", "Updated", "Log"],     # row 2 = header
        ["Add login", "add-login", "done", "2026-01-01", "ok"],
        ["", "", "", "", ""],            # blank -> skipped
        ["Add logout", "", "", "", ""],  # actionable
    ]
    tab = parse_grid("repo1", grid)
    assert tab.repo_binding == "/srv/myrepo"
    assert tab.branch == "main"
    assert len(tab.rows) == 2
    assert tab.rows[0].task == "Add login"
    assert tab.rows[0].actionable is False        # status done
    assert tab.rows[1].task == "Add logout"
    assert tab.rows[1].actionable is True         # blank status + has task


def test_mock_backend_roundtrip_and_bootstrap(tmp_path: Path):
    be = MockBackend(str(tmp_path / "m.json"))
    be.write_cell("repoA", C.CONFIG_ROW, 2, "/srv/a")   # B1
    be.ensure_schema("repoA")
    tab = be.read_tab("repoA")
    assert tab.repo_binding == "/srv/a"
    # header row written by bootstrap
    grid = json.loads((tmp_path / "m.json").read_text())["tabs"]["repoA"]["grid"]
    assert grid[C.HEADER_ROW - 1][0] == C.HEADERS[0]            # "Задача"
    assert grid[C.CONFIG_ROW - 1][0] == C.CONFIG_LABEL_REPO     # "Репозиторий"
    # write a task + status back
    be.write_cell("repoA", 4, C.COL_TASK, "do thing")
    be.write_cell("repoA", 4, C.COL_STATUS, "working")
    tab2 = be.read_tab("repoA")
    assert tab2.rows[0].task == "do thing"
    assert tab2.rows[0].status == "working"


def test_parse_structured_output_envelope():
    env = json.dumps({"result": "ignored", "structured_output": {"outcome": "implemented", "summary": "done", "spec_id": "add-x"}})
    obj = _parse(env)
    assert obj["outcome"] == "implemented"
    assert obj["spec_id"] == "add-x"


def test_parse_plain_and_nested_json():
    assert _parse(json.dumps({"outcome": "failed", "summary": "x"}))["outcome"] == "failed"
    nested = json.dumps({"result": json.dumps({"outcome": "blocked", "summary": "no os"})})
    assert _parse(nested)["outcome"] == "blocked"
    assert _parse("not json at all") is None


def test_rate_limit_detection():
    assert _RATE_RE.search("Error: rate limit exceeded")
    assert _RATE_RE.search("API error 429 overloaded")
    assert _RATE_RE.search("billing: insufficient credit")
    assert not _RATE_RE.search("implemented the feature, tests pass")


def test_git_url_detection():
    assert _looks_like_git_url("git@github.com:x/y.git")
    assert _looks_like_git_url("https://github.com/x/y.git")
    assert _looks_like_git_url("ssh://git@h/x.git")
    assert not _looks_like_git_url("/srv/local")
    assert not _looks_like_git_url("barename")


def test_parse_grid_reads_tries():
    grid = [
        ["REPO_PATH", "/srv/r", "BRANCH", "main"],
        ["Task", "Spec", "Status", "Updated", "Log", "Tries"],   # row 2 = header
        ["t1", "", "queued", "", "", "2"],
        ["t2", "", "queued", "", "", ""],          # blank -> default
    ]
    tab = parse_grid("r", grid)
    assert tab.rows[0].tries == 2
    assert tab.rows[1].tries == 0
    # Priority is gone: TaskRow no longer carries it.
    assert not hasattr(tab.rows[0], "priority")


def test_legacy_old_layout_migration_drops_vision_detail_and_priority(tmp_path: Path):
    """A full old-layout tab — VISION row at row 2, 8-column header (Detail at D,
    Priority at H) at row 3 — is realigned in one cycle: the vision-row migration
    deletes row 2 (header -> row 2, data -> row 3), then the Detail migration drops D
    (Priority lands in G), then the Priority migration drops G. Tries survives in F."""
    be = MockBackend(str(tmp_path / "m.json"))
    title = "legacy"
    # Old layout, written at LITERAL rows (config=1, VISION=2, header=3, data=4).
    be.write_cell(title, 1, 1, "REPO_PATH")
    be.write_cell(title, 1, 2, "/srv/legacy")
    be.write_cell(title, 2, 1, "VISION")
    be.write_cell(title, 2, 2, "win the market")
    old_headers = ["Task", "Spec", "Status", "Detail",
                   "Updated", "Log", "Tries", "Priority"]
    for i, h in enumerate(old_headers, start=1):
        be.write_cell(title, 3, i, h)
    be.write_cell(title, 4, 1, "do thing")      # A  Task
    be.write_cell(title, 4, 4, "acc criteria")  # D  Detail -> dropped
    be.write_cell(title, 4, 5, "2026-05-05")    # E  Updated
    be.write_cell(title, 4, 7, "3")             # G  Tries
    be.write_cell(title, 4, 8, "9")             # H  Priority -> dropped

    tab = be.read_tab(title)   # first read runs all three migrations
    assert tab.rows[0].task == "do thing"
    assert tab.rows[0].updated == "2026-05-05"   # was E(5) -> now D(4)
    assert tab.rows[0].tries == 3                # was G(7) -> now F(6)
    assert not hasattr(tab.rows[0], "priority")

    grid = json.loads((tmp_path / "m.json").read_text())["tabs"][title]["grid"]
    assert grid[C.HEADER_ROW - 1] == C.HEADERS                 # header now at row 2
    assert grid[C.CONFIG_ROW - 1][0] == C.CONFIG_LABEL_REPO    # relabelled to Russian
    assert grid[C.HEADER_ROW - 1][0] != "VISION"               # VISION row gone
    assert "acc criteria" not in grid[C.FIRST_TASK_ROW - 1]
    assert "9" not in grid[C.FIRST_TASK_ROW - 1]

    # Idempotent: a fresh backend re-runs ensure_schema but changes nothing more.
    tab2 = MockBackend(str(tmp_path / "m.json")).read_tab(title)
    assert tab2.rows[0].tries == 3
    grid2 = json.loads((tmp_path / "m.json").read_text())["tabs"][title]["grid"]
    assert grid2[C.HEADER_ROW - 1] == C.HEADERS


def test_drop_vision_row_migration_realigns_and_is_idempotent(tmp_path: Path):
    """A tab on the current-columns-but-old-rows layout — VISION row at row 2, the
    6-column header at row 3 — has its row 2 deleted so the header realigns to row 2
    and tasks to row 3. Idempotent on re-read."""
    be = MockBackend(str(tmp_path / "m.json"))
    title = "visionrow"
    be.write_cell(title, 1, 1, "REPO_PATH")
    be.write_cell(title, 1, 2, "/srv/visionrow")
    be.write_cell(title, 2, 1, "VISION")
    be.write_cell(title, 2, 2, "our vision")
    for i, h in enumerate(C.HEADERS, start=1):
        be.write_cell(title, 3, i, h)
    be.write_cell(title, 4, 1, "do thing")      # A  Task
    be.write_cell(title, 4, 4, "2026-05-05")    # D  Updated
    be.write_cell(title, 4, 6, "2")             # F  Tries

    tab = be.read_tab(title)   # first read runs the vision-row migration
    assert tab.rows[0].task == "do thing"
    assert tab.rows[0].updated == "2026-05-05"
    assert tab.rows[0].tries == 2

    grid = json.loads((tmp_path / "m.json").read_text())["tabs"][title]["grid"]
    assert grid[C.HEADER_ROW - 1] == C.HEADERS                 # header now at row 2
    assert "our vision" not in [c for row in grid for c in row]  # vision text gone

    # Idempotent: a fresh backend re-runs ensure_schema but deletes nothing more.
    tab2 = MockBackend(str(tmp_path / "m.json")).read_tab(title)
    assert tab2.rows[0].task == "do thing"
    assert tab2.rows[0].tries == 2
    grid2 = json.loads((tmp_path / "m.json").read_text())["tabs"][title]["grid"]
    assert grid2[C.HEADER_ROW - 1] == C.HEADERS


def test_russianize_migration_relabels_in_place_and_is_idempotent(tmp_path: Path):
    """A tab still on the old English labels (`REPO_PATH`/`BRANCH`/`Task,…`) is
    relabelled to the Russian ones in place, the B1 binding + task data survive, and a
    second read changes nothing."""
    be = MockBackend(str(tmp_path / "m.json"))
    title = "oldlabels"
    be.write_cell(title, 1, 1, "REPO_PATH")
    be.write_cell(title, 1, 2, "/srv/x")          # B1 binding — must survive
    be.write_cell(title, 1, 3, "BRANCH")
    be.write_cell(title, 1, 4, "main")
    for i, h in enumerate(["Task", "Spec", "Status", "Updated", "Log", "Tries"], start=1):
        be.write_cell(title, 2, i, h)
    be.write_cell(title, 3, 1, "do thing")
    be.write_cell(title, 3, 3, "queued")

    tab = be.read_tab(title)                       # first read runs _migrate_russianize
    assert tab.repo_binding == "/srv/x"
    assert tab.rows[0].task == "do thing" and tab.rows[0].status == "queued"

    grid = json.loads((tmp_path / "m.json").read_text())["tabs"][title]["grid"]
    assert grid[C.CONFIG_ROW - 1][0] == C.CONFIG_LABEL_REPO     # A1 "Репозиторий"
    assert grid[C.CONFIG_ROW - 1][2] == C.CONFIG_LABEL_BRANCH   # C1 "Ветка"
    assert grid[C.CONFIG_ROW - 1][1] == "/srv/x"               # B1 untouched
    assert grid[C.HEADER_ROW - 1] == C.HEADERS                 # Russian header

    # Idempotent: a fresh backend re-runs ensure_schema but rewrites nothing more.
    MockBackend(str(tmp_path / "m.json")).read_tab(title)
    grid2 = json.loads((tmp_path / "m.json").read_text())["tabs"][title]["grid"]
    assert grid2 == grid


def test_status_colors_cover_all_statuses_and_build_one_rule_each():
    from sheet_agent.sheets import _status_cf_requests
    # Every status the daemon/human can set has a colour.
    assert set(C.STATUS_COLORS) == set(C.ALL_STATUSES)
    reqs = _status_cf_requests(999)
    assert len(reqs) == len(C.ALL_STATUSES)
    statuses = {r["addConditionalFormatRule"]["rule"]["booleanRule"]
                ["condition"]["values"][0]["userEnteredValue"] for r in reqs}
    assert statuses == set(C.ALL_STATUSES)
    # Indices are sequential from 0 so they apply cleanly after the existing rules
    # are cleared (delete index 0 N times).
    assert [r["addConditionalFormatRule"]["index"] for r in reqs] == list(range(len(reqs)))
    # Each rule paints the Status column with a background colour.
    for r in reqs:
        fmt = r["addConditionalFormatRule"]["rule"]["booleanRule"]["format"]
        assert "backgroundColor" in fmt


def test_config_validate_autonomy_and_concurrency():
    assert C.Config(backend="mock", autonomy="gated").validate() == []
    assert any("AUTONOMY" in p for p in C.Config(backend="mock", autonomy="bogus").validate())
    assert any("CONCURRENT" in p for p in
               C.Config(backend="mock", max_concurrent_agents=0).validate())
    # MAX_ATTEMPTS=0 would dead-letter every task on its first dispatch — must surface.
    assert any("MAX_ATTEMPTS" in p for p in
               C.Config(backend="mock", max_attempts=0).validate())
    # MAX_CONCURRENT_CHATS<=0 is silently clamped to 1 at runtime — surface the typo.
    assert any("MAX_CONCURRENT_CHATS" in p for p in
               C.Config(backend="mock", max_concurrent_chats=0).validate())


def test_task_prompt_phases():
    spec = _task_prompt("do x", "", "gated", False, phase="spec")
    assert "spec_ready" in spec and "do NOT write" in spec
    impl = _task_prompt("do x", "", "gated", False, phase="implement", spec_id="add-x")
    assert "add-x" in impl and "ALREADY EXISTS" in impl and "Do NOT create" in impl
    full = _task_prompt("do x", "", "ship", False)
    assert "deploy" in full


def _mock_orch(tmp_path: Path, **kw) -> Orchestrator:
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"), **kw)
    return Orchestrator(cfg)


def test_plan_row_attempt_cap_and_reset(tmp_path: Path):
    orch = _mock_orch(tmp_path, max_attempts=3)
    repo = tmp_path
    # under the cap -> dispatch, next attempt stamped
    w = orch._plan_row("t", repo, False, TaskRow(row=4, task="x", status="queued", tries=2), "x", "")
    assert w is not None and w.next_tries == 3 and w.phase == "full"
    # at the cap -> dead-letter (None)
    assert orch._plan_row("t", repo, False,
                          TaskRow(row=4, task="x", status="queued", tries=3), "x", "") is None
    # retry resets the budget even past the cap
    w2 = orch._plan_row("t", repo, False, TaskRow(row=4, task="x", status="retry", tries=9), "x", "")
    assert w2 is not None and w2.next_tries == 1


def test_discover_repos(tmp_path: Path):
    (tmp_path / "alpha" / ".git").mkdir(parents=True)
    (tmp_path / "beta" / ".git").mkdir(parents=True)
    (tmp_path / "beta" / "openspec").mkdir()
    (tmp_path / "notrepo").mkdir()                       # no .git -> skipped
    (tmp_path / "auto" / "dealer" / ".git").mkdir(parents=True)   # nested repo
    (tmp_path / "auto" / "dealer" / "sub" / ".git").mkdir(parents=True)  # don't descend
    # deep nesting: category/sub-category/repo (e.g. auto/channels/b2c/acme-ai)
    (tmp_path / "auto" / "channels" / "b2c" / "acme-ai" / ".git").mkdir(parents=True)
    (tmp_path / "auto" / "channels" / "b2b" / "chat" / ".git").mkdir(parents=True)
    cfg = C.Config(repo_search_roots=(str(tmp_path),))
    repos = {r.name: r for r in discover(cfg)}
    assert set(repos) == {
        "alpha", "beta", "auto/dealer",
        "auto/channels/b2c/acme-ai", "auto/channels/b2b/chat",
    }
    assert repos["beta"].has_openspec and not repos["alpha"].has_openspec
    # nested name binds through resolve()'s bare-name branch (canonicalised)
    assert resolve("auto/dealer", cfg).path == (tmp_path / "auto" / "dealer").resolve()


def test_discover_descends_into_gitignore_category_folder(tmp_path: Path):
    # A non-git parent is a category folder — its OWN .gitignore (workspace junk
    # rules) does NOT make it a repo. We descend in and surface the nested
    # checkouts. Regression for the wish/example case: beelink-example.ru/
    # is exactly such a folder holding two real checkouts the user wants to add.
    (tmp_path / "proj" / ".gitignore").parent.mkdir(parents=True)
    (tmp_path / "proj" / ".gitignore").write_text(".env\n")
    (tmp_path / "proj" / "a" / ".git").mkdir(parents=True)
    (tmp_path / "proj" / "b" / ".git").mkdir(parents=True)
    cfg = C.Config(repo_search_roots=(str(tmp_path),))
    names = {r.name for r in discover(cfg)}
    assert names == {"proj/a", "proj/b"}


def test_discover_honours_repo_ignore(tmp_path: Path):
    (tmp_path / "keep" / ".git").mkdir(parents=True)
    (tmp_path / "legacy" / ".git").mkdir(parents=True)
    (tmp_path / "auto" / "old" / ".git").mkdir(parents=True)
    (tmp_path / "auto" / "new" / ".git").mkdir(parents=True)
    cfg = C.Config(repo_search_roots=(str(tmp_path),),
                   repo_ignore=("legacy", "auto/old"))
    names = {r.name for r in discover(cfg)}
    assert names == {"keep", "auto/new"}


def test_refresh_repos_rewrites_only_on_change(tmp_path: Path, monkeypatch):
    orch = _mock_orch(tmp_path)
    calls = {"n": 0}
    real = orch.backend.ensure_repos_tab

    def counting(repos):
        calls["n"] += 1
        real(repos)

    monkeypatch.setattr(orch.backend, "ensure_repos_tab", counting)

    from sheet_agent import repo as repolib
    from sheet_agent.repo import RepoInfo
    seq = [[RepoInfo("alpha", tmp_path / "alpha", False)]]
    monkeypatch.setattr(repolib, "discover", lambda cfg: list(seq[0]))

    orch._refresh_repos()                       # first publish
    assert calls["n"] == 1
    grid = json.loads((tmp_path / "m.json").read_text())["tabs"][C.REPOS_TAB]["grid"]
    assert [row[0] for row in grid] == ["Репо", "alpha"]

    orch._refresh_repos()                       # unchanged -> no rewrite
    assert calls["n"] == 1

    seq[0] = []                                 # repo deleted -> rewrite, row gone
    orch._refresh_repos()
    assert calls["n"] == 2
    grid = json.loads((tmp_path / "m.json").read_text())["tabs"][C.REPOS_TAB]["grid"]
    assert [row[0] for row in grid] == ["Репо"]


def test_meta_tabs_are_skipped(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    # _repos / any underscore tab must never be processed as a repo tab
    assert orch._process_tab(C.REPOS_TAB) == 0
    assert orch._process_tab("_notes") == 0


def test_plan_row_gated_phases(tmp_path: Path):
    orch = _mock_orch(tmp_path, autonomy="gated")
    repo = tmp_path
    spec = orch._plan_row("t", repo, False, TaskRow(row=4, task="x", status="queued"), "x", "")
    assert spec.phase == "spec"
    impl = orch._plan_row("t", repo, False,
                          TaskRow(row=4, task="x", status="approved", spec="add-x"), "x", "")
    assert impl.phase == "implement" and impl.spec_id == "add-x" and impl.next_tries == 1
    # approved without a spec id -> blocked (None)
    assert orch._plan_row("t", repo, False,
                          TaskRow(row=4, task="x", status="approved"), "x", "") is None


def test_parse_returns_last_valid_object_from_noise():
    # Greedy `{.*}` would span both objects and fail; the right-to-left raw_decode
    # scan must isolate the LAST well-formed structured object.
    noisy = ('boot log\n{"outcome": "failed", "summary": "first attempt"}\n'
             'retry…\n{"outcome": "implemented", "summary": "second"} trailing junk')
    obj = _parse(noisy)
    assert obj["outcome"] == "implemented" and obj["summary"] == "second"


def test_openspec_gate_blocks_repo_without_openspec(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    be = orch.backend
    repo = tmp_path / "plainrepo"
    repo.mkdir()                       # exists, but no openspec/
    be.write_cell("t", C.CONFIG_ROW, 2, str(repo))   # B1 binding
    be.ensure_schema("t")
    be.write_cell("t", C.FIRST_TASK_ROW, C.COL_TASK, "do x")
    be.write_cell("t", C.FIRST_TASK_ROW, C.COL_STATUS, "queued")

    assert orch._collect_tab("t") == []                      # nothing dispatched
    assert be.read_tab("t").rows[0].status == C.ST_BLOCKED   # row blocked by the gate


def test_openspec_gate_allows_with_auto_init(tmp_path: Path):
    orch = _mock_orch(tmp_path, auto_openspec_init=True)
    be = orch.backend
    repo = tmp_path / "plainrepo"
    repo.mkdir()
    be.write_cell("t", C.CONFIG_ROW, 2, str(repo))
    be.ensure_schema("t")
    be.write_cell("t", C.FIRST_TASK_ROW, C.COL_TASK, "do x")
    be.write_cell("t", C.FIRST_TASK_ROW, C.COL_STATUS, "queued")

    items = orch._collect_tab("t")
    assert len(items) == 1 and items[0].allow_init is True


def test_single_instance_lock(tmp_path: Path):
    lock = tmp_path / "agent.lock"
    with SingleInstance(lock):
        # a second acquire of the same lock must refuse
        with pytest.raises(SystemExit):
            with SingleInstance(lock):
                pass
    # released on exit -> re-acquirable
    with SingleInstance(lock):
        pass


def test_resolve_path_and_openspec(tmp_path: Path):
    repo = tmp_path / "proj"
    (repo / "openspec").mkdir(parents=True)
    cfg = C.Config(repo_search_roots=(str(tmp_path),))
    r = resolve(str(repo), cfg)
    assert r.ok and r.path == repo.resolve()
    assert has_openspec(repo)
    # bare name search
    r2 = resolve("proj", cfg)
    assert r2.ok and r2.path == repo.resolve()
    # missing
    assert resolve("/nope/nada", cfg).ok is False
    assert resolve("", cfg).ok is False


def test_resolve_canonicalises_so_one_repo_has_one_lock_key(tmp_path: Path):
    """Different binding forms for the SAME physical repo MUST resolve to one
    identical path, so the orchestrator's per-repo `_inflight` lock dedupes them and
    never runs two agents in one working tree. Regression for the non-canonical key."""
    repo = tmp_path / "proj"
    (repo / "openspec").mkdir(parents=True)
    cfg = C.Config(repo_search_roots=(str(tmp_path),))
    abs_plain = resolve(str(repo), cfg)
    abs_trailing = resolve(str(repo) + "/", cfg)            # trailing slash
    abs_dotdot = resolve(str(repo / "openspec" / ".."), cfg)  # ..-detour to same dir
    bare = resolve("proj", cfg)                             # bare-name branch
    paths = {abs_plain.path, abs_trailing.path, abs_dotdot.path, bare.path}
    assert len(paths) == 1, f"expected one canonical key, got {paths}"
