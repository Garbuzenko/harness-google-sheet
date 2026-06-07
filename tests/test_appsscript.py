"""Offline STRUCTURAL tests for Stage 5 — sheet-buttons-appsscript.

The Apps Script button layer lives in `appsscript/` (clasp project, git = source of
truth). It runs in Google's cloud and can ONLY append intent rows to the `_control`
tab (openspec/specs/sheet-control-queue). There is no way to run Apps Script offline, so these tests
assert the CONTRACT by parsing/grepping the `.gs` source — no Google, no clasp, no
network, no real `claude`.

The Python daemon is NOT exercised or modified here; Stage 5 is the cloud layer only.

Run: pytest -q
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
APPSSCRIPT_DIR = REPO_ROOT / "appsscript"
CODE_GS = APPSSCRIPT_DIR / "Code.gs"


@pytest.fixture(scope="module")
def src() -> str:
    return CODE_GS.read_text(encoding="utf-8")


# --- AC: appsscript/ clasp project committed to git ------------------------
def test_appsscript_dir_exists():
    assert APPSSCRIPT_DIR.is_dir(), "appsscript/ directory must exist in git"


def test_clasp_and_manifest_files_present():
    manifest = APPSSCRIPT_DIR / "appsscript.json"
    clasp = APPSSCRIPT_DIR / ".clasp.json"
    assert manifest.is_file(), "appsscript/appsscript.json manifest must exist"
    json.loads(manifest.read_text(encoding="utf-8"))
    # .clasp.json carries the Apps Script id + the bound spreadsheet id, so it is
    # gitignored (a private, per-deployment local binding — never committed). It
    # only exists once you run `clasp clone`/`clasp create`. Validate it IF present.
    if clasp.is_file():
        json.loads(clasp.read_text(encoding="utf-8"))


def test_gs_source_present():
    gs_files = list(APPSSCRIPT_DIR.glob("*.gs"))
    assert gs_files, "at least one .gs Apps Script source file must exist"
    assert CODE_GS.is_file(), "appsscript/Code.gs must exist"


# --- AC: onOpen builds the 🤖 Supervisor menu with EXACTLY 3 addItem bindings
def test_menu_title_is_exactly_supervisor(src: str):
    # The menu is created via SpreadsheetApp.getUi().createMenu('🤖 Supervisor').
    # Tolerate whitespace/newlines between the chained calls (idiomatic fluent style).
    assert re.search(
        r"SpreadsheetApp\.getUi\(\)\s*\.createMenu\(\s*['\"]🤖 Supervisor['\"]\s*\)", src
    ), "menu must be created via SpreadsheetApp.getUi().createMenu titled exactly '🤖 Supervisor'"


def test_onOpen_defined(src: str):
    assert re.search(r"\bfunction\s+onOpen\s*\(", src), "onOpen(e) must be defined"


def _additem_handlers(src: str) -> list[str]:
    """Return the handler-function names bound by each .addItem(label, handler)."""
    # .addItem('label', 'handlerName')
    return re.findall(r"\.addItem\(\s*['\"][^'\"]*['\"]\s*,\s*['\"]([A-Za-z0-9_]+)['\"]\s*\)", src)


def test_exactly_four_addItem_bindings(src: str):
    handlers = _additem_handlers(src)
    assert len(handlers) == 4, f"menu must have EXACTLY four addItem bindings, got {handlers!r}"


def test_four_dialog_handlers_wired_and_defined(src: str):
    handlers = _additem_handlers(src)
    # the four intents: add repo, create repo, run skill, share repos
    assert "showAddRepoDialog" in handlers
    assert "showCreateRepoDialog" in handlers
    assert "showRunSkillDialog" in handlers
    assert "showShareReposDialog" in handlers
    # every bound handler must actually be defined in the source
    for h in handlers:
        assert re.search(rf"\bfunction\s+{re.escape(h)}\s*\(", src), \
            f"addItem-bound handler {h} must be defined"


def test_menu_added_to_ui(src: str):
    assert ".addToUi()" in src, "the menu must be added to the UI via addToUi()"


# --- AC: A-E only, status=pending, never result (F) ------------------------
def test_append_helper_writes_only_A_to_E(src: str):
    # The shared append helper appends an array of EXACTLY 5 fields:
    # [id, ts, action, JSON.stringify(args), CTL_PENDING] — no 6th (result/F).
    m = re.search(r"appendRow\(\s*\[(.*?)\]\s*\)", src, re.DOTALL)
    assert m, "appendRow([...]) must exist (the _control append)"
    fields = [f.strip() for f in m.group(1).split(",")]
    assert len(fields) == 5, f"appendRow must write EXACTLY 5 cells (A-E), got {fields!r}"
    # E (status) must be pending, and result (F) must NOT be appended.
    assert any("PENDING" in f or "pending" in f for f in fields), \
        "the 5th appended field (E/status) must be pending"


def test_status_pending_constant(src: str):
    assert re.search(r"CTL_PENDING\s*=\s*'pending'", src) \
        or re.search(r'CTL_PENDING\s*=\s*"pending"', src), \
        "status pending must be set on appended rows"


def test_id_is_ts_dash_rand(src: str):
    # id = <ts>-<rand> per §4.1 — assert ts and a random component are concatenated.
    assert "Math.random()" in src, "id must include a random component (<ts>-<rand>)"
    assert re.search(r"var\s+id\s*=", src), "an id field must be constructed for column A"


def test_never_writes_result_column_F(src: str):
    # Defensive: no setValue/append that targets a 6th column / 'result'.
    # The only sheet mutation is appendRow with 5 fields (asserted above).
    assert "setValue('result'" not in src
    assert 'setValue("result"' not in src


# --- AC: append targets the _control tab, never a repo tab's A/D/H/B1 -------
def test_control_tab_constant(src: str):
    assert re.search(r"CONTROL_TAB\s*=\s*'_control'", src) \
        or re.search(r'CONTROL_TAB\s*=\s*"_control"', src), \
        "the append must target the _control tab"
    assert "getSheetByName(CONTROL_TAB)" in src, \
        "the append helper must resolve the _control sheet by name"


def test_no_writes_to_repo_tab_human_cells(src: str):
    """Apps Script must never write a repo tab's human-owned A/D/H or its B1 binding.
    The ONLY mutation seam is appendRow on _control; assert there is no setValue /
    setValues / range write to B1/A1/D/H of a repo tab."""
    # No range writes at all (the contract is append-only to _control).
    assert ".setValue(" not in src, "dialogs must not setValue — append-only to _control"
    assert ".setValues(" not in src, "dialogs must not setValues — append-only to _control"
    # exactly one appendRow (the _control intent), nothing else mutates the sheet.
    assert src.count("appendRow(") == 1, "there must be exactly one appendRow (the _control intent)"


# --- AC: add-repo dialog — reads _repos!A2:C, args {repo, path} -------------
def test_add_repo_reads_repos_A2_C(src: str):
    assert re.search(r"REPOS_TAB\s*=\s*'_repos'", src) \
        or re.search(r'REPOS_TAB\s*=\s*"_repos"', src), \
        "add-repo must reference the _repos tab"
    assert "'A2:C'" in src or '"A2:C"' in src, "add-repo must read the _repos!A2:C range"


def test_add_repo_action_and_arg_shape(src: str):
    assert re.search(r"ACTION_ADD_REPO\s*=\s*'add_repo'", src) \
        or re.search(r'ACTION_ADD_REPO\s*=\s*"add_repo"', src), \
        "action add_repo must be defined"
    # one add_repo row per selected repo with args {repo, path}
    m = re.search(r"appendControlRow_\(\s*ACTION_ADD_REPO\s*,\s*\{([^}]*)\}", src)
    assert m, "add-repo must append a control row with ACTION_ADD_REPO and an args object"
    args = m.group(1)
    assert "repo" in args and "path" in args, \
        f"add_repo args must contain repo and path, got {{{args}}}"


# --- AC: create-repo dialog — confirm precedes append, args {name,template,vision}
def test_create_repo_action_and_arg_shape(src: str):
    assert re.search(r"ACTION_CREATE_REPO\s*=\s*'create_repo'", src) \
        or re.search(r'ACTION_CREATE_REPO\s*=\s*"create_repo"', src), \
        "action create_repo must be defined"
    m = re.search(r"appendControlRow_\(\s*ACTION_CREATE_REPO\s*,\s*\{([^}]*)\}", src)
    assert m, "create-repo must append a control row with ACTION_CREATE_REPO"
    args = m.group(1)
    for key in ("name", "template", "vision"):
        assert key in args, f"create_repo args must contain {key}, got {{{args}}}"


def test_create_repo_confirm_gate_text(src: str):
    # The irreversibility gate: a confirm whose text includes "Создать beelink-".
    assert "Создать beelink-" in src, \
        "create-repo must have a confirm dialog whose text includes 'Создать beelink-'"


def test_create_repo_confirm_precedes_append(src: str):
    """The confirm must precede the create_repo append. The append happens inside the
    server callback enqueueCreateRepo_, which is invoked by the client ONLY after the
    confirm() returns true — assert that ordering in the dialog HTML/JS."""
    confirm_idx = src.find("Создать beelink-")
    # the client calls enqueueCreateRepo (which appends) only after the confirm
    call_idx = src.find("enqueueCreateRepo")
    assert confirm_idx != -1 and call_idx != -1
    # the confirm() guard must appear before the call that triggers the append
    # find the confirm() statement and the subsequent enqueue call
    guarded = re.search(
        r"confirm\([^)]*Создать beelink-[\s\S]*?enqueueCreateRepo\b",
        src,
    )
    assert guarded, "the create_repo append (enqueueCreateRepo) must come AFTER the confirm()"


# --- AC: run-skill dialog — reads _skills, action run_skill, args {skill,tab,detail}
def test_run_skill_reads_skills_tab(src: str):
    assert re.search(r"SKILLS_TAB\s*=\s*'_skills'", src) \
        or re.search(r'SKILLS_TAB\s*=\s*"_skills"', src), \
        "run-skill must reference the _skills tab"
    assert "getSheetByName(SKILLS_TAB)" in src, \
        "run-skill must resolve the _skills sheet by name"
    assert "'A2:B'" in src or '"A2:B"' in src or "'A2:B' +" in src, \
        "run-skill must read the _skills!A2:B range"


def test_run_skill_reads_active_sheet_title(src: str):
    assert "getActiveSheet()" in src, "run-skill must read the active sheet"
    assert ".getName()" in src, "run-skill must read the active sheet's title (getName)"


def test_run_skill_action_and_arg_shape(src: str):
    assert re.search(r"ACTION_RUN_SKILL\s*=\s*'run_skill'", src) \
        or re.search(r'ACTION_RUN_SKILL\s*=\s*"run_skill"', src), \
        "action run_skill must be defined"
    m = re.search(r"appendControlRow_\(\s*ACTION_RUN_SKILL\s*,\s*\{([^}]*)\}", src)
    assert m, "run-skill must append a control row with ACTION_RUN_SKILL"
    args = m.group(1)
    for key in ("skill", "tab", "detail"):
        assert key in args, f"run_skill args must contain {key}, got {{{args}}}"


# --- AC: share-repos dialog — action share_repos, args {recipient,repos,autonomy}
def test_share_repos_action_and_arg_shape(src: str):
    assert re.search(r"ACTION_SHARE_REPOS\s*=\s*'share_repos'", src) \
        or re.search(r'ACTION_SHARE_REPOS\s*=\s*"share_repos"', src), \
        "action share_repos must be defined"
    m = re.search(r"appendControlRow_\(\s*ACTION_SHARE_REPOS\s*,\s*\{([^}]*)\}",
                  src, re.DOTALL)
    assert m, "share-repos must append a control row with ACTION_SHARE_REPOS"
    args = m.group(1)
    for key in ("recipient", "repos", "autonomy"):
        assert key in args, f"share_repos args must contain {key}, got {{{args}}}"


def test_share_repos_dialog_wired(src: str):
    assert re.search(r"\bfunction\s+showShareReposDialog\s*\(", src)
    assert re.search(r"\bfunction\s+enqueueShareRepos\s*\(", src)


# --- AC: client→server bridge functions must be CALLABLE (not private) ------
def test_google_script_run_targets_are_public_and_defined(src: str):
    """In Apps Script a function whose name ends with `_` is PRIVATE and CANNOT be
    invoked via `google.script.run` — the call silently no-ops. Regression: the
    add-repo / create-repo dialogs targeted `enqueueAddRepos_` / `enqueueCreateRepo_`
    (trailing underscore) so the buttons did nothing. A method-style call `.name(`
    to one of our own defined functions is a google.script.run target — it must be
    public (no trailing `_`)."""
    defined = set(re.findall(r"function\s+(\w+)\s*\(", src))
    targets = {name for name in re.findall(r"\.(\w+)\s*\(", src) if name in defined}
    assert targets, "expected google.script.run to target at least one defined function"
    for fn in sorted(targets):
        assert not fn.endswith("_"), (
            f"google.script.run target {fn!r} ends with '_' (PRIVATE in Apps Script "
            "— the client bridge will not call it)")
