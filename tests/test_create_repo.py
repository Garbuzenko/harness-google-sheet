"""Offline tests for Stage 4 — create-new-repo.

The DETERMINISTIC engine `scripts/create_beelink_repo.sh` plus the `create_repo`
control handler. EVERY external seam is mocked/stubbed — curl (GitHub + deployer),
git (init/remote/push), the PAT file, the template source, the projects root — so
NOTHING ever hits the network or the real `$HOME/projects/beelink`.

Run: pytest -q
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from sheet_agent import config as C
from sheet_agent import orchestrator as orch_mod
from sheet_agent.orchestrator import Orchestrator, register_control_handler
from sheet_agent.sheets import MockBackend

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "create_beelink_repo.sh"
SKILL = REPO_ROOT / ".claude" / "skills" / "create-beelink-repo" / "SKILL.md"


# --------------------------------------------------------------------------
# Test harness: a fake template + stub curl/git on PATH so no network/real git.
# --------------------------------------------------------------------------
def _make_template(tmp_path: Path) -> Path:
    """A minimal fake of $HOME/projects/init/init_project, INCLUDING a
    .env.example that the script must strip on copy."""
    tpl = tmp_path / "template"
    (tpl / "deploy").mkdir(parents=True, exist_ok=True)
    (tpl / "docs").mkdir(exist_ok=True)
    (tpl / ".env.example").write_text("PROJECT_NAME=my-project\nDB_URL=x\n")
    (tpl / ".gitignore").write_text(".env\nnode_modules/\n")
    (tpl / "README.md").write_text("# template\n")
    (tpl / "deploy" / "deploy.sh").write_text("#!/bin/bash\necho deploy\n")
    return tpl


def _stub_bin(dir_: Path, name: str, body: str) -> Path:
    p = dir_ / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


# A curl stub that records every invocation and answers the two GitHub calls
# (existence GET -> 404 by default, create POST -> ok) and the deployer POST.
_CURL_STUB = r"""
set -e
echo "CURL $*" >> "$STUB_LOG"
# Existence check uses -w '%{http_code}' (and -o /dev/null) — emit the http code.
for a in "$@"; do
  if [ "$a" = "%{http_code}" ]; then
    echo -n "${GH_EXISTS_CODE:-404}"
    exit 0
  fi
done
# Deployer registration POST -> a JSON env object.
case "$*" in
  *"/projects"*) echo '{"DATABASE_URL":"postgres://x","QDRANT_URL":"http://q"}' ;;
  *"api.github.com/user/repos"*) echo '{"clone_url":"x"}' ;;
  *) echo '{}' ;;
esac
"""

# A git stub that records calls and never touches a real remote.
_GIT_STUB = r"""
echo "GIT $*" >> "$STUB_LOG"
exit 0
"""


def _run_script(tmp_path: Path, *, name: str, vision: str = "v",
                projects_root: Path | None = None, gh_exists_code: str = "404",
                real_git: bool = False, extra_env: dict | None = None):
    """Invoke the engine with stubbed curl (+optionally git) on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "stub.log"
    log.write_text("")
    _stub_bin(bindir, "curl", _CURL_STUB)
    tpl = _make_template(tmp_path)
    proot = projects_root if projects_root is not None else (tmp_path / "projects")
    proot.mkdir(exist_ok=True)
    token = tmp_path / "gh-token"
    token.write_text("ghp_FAKE_TOKEN\n")

    env = dict(os.environ)
    env.update({
        "PATH": f"{bindir}:{env['PATH']}",
        "PROJECTS_ROOT": str(proot),
        "TEMPLATE_DIR": str(tpl),
        "GH_TOKEN_FILE": str(token),
        "DEPLOYER_URL": "http://deployer.invalid:8000",
        "GITHUB_OWNER": "Garbuzenko",
        "CURL_BIN": str(bindir / "curl"),
        "STUB_LOG": str(log),
        "GH_EXISTS_CODE": gh_exists_code,
        # Keep git deterministic + offline; a stub avoids any chance of a push.
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        # Use a fake openspec so we never depend on it being installed.
        "OPENSPEC_BIN": str(_stub_bin(bindir, "openspec", "exit 0\n")),
    })
    if not real_git:
        env["GIT_BIN"] = str(_stub_bin(bindir, "git", _GIT_STUB))
    if extra_env:
        env.update(extra_env)

    proc = subprocess.run(["bash", str(SCRIPT), "--name", name,
                           "--vision", vision],
                          capture_output=True, text=True, env=env, timeout=60)
    return proc, proot, log


# --------------------------------------------------------------------------
# AC #1: invalid name -> non-zero, nothing created
# --------------------------------------------------------------------------
def test_invalid_name_exits_nonzero_creates_nothing(tmp_path: Path):
    proc, proot, _ = _run_script(tmp_path, name="Foo_Bar")
    assert proc.returncode != 0
    # No directory was created under the projects root.
    assert list(proot.iterdir()) == []


def test_invalid_name_variants(tmp_path: Path):
    for bad in ["Foo", "-leading", "has space", "UPPER", "a/b", "trailing-",
                "dot.", ".dot"]:
        proc, proot, _ = _run_script(tmp_path, name=bad)
        assert proc.returncode != 0, f"{bad!r} should be rejected"
        assert not (proot / f"beelink-{bad}").exists()


def test_dotted_domain_style_name_accepted(tmp_path: Path):
    """A domain-style bare name with internal dots (e.g. `foo.ru`) is valid:
    the original `^[a-z0-9][a-z0-9-]*$` regex wrongly rejected it, so the click
    on 'создать репо' for foo.ru created nothing. It must now succeed."""
    proc, proot, _ = _run_script(tmp_path, name="foo.ru")
    assert proc.returncode == 0, proc.stderr
    dest = proot / "beelink-foo.ru"
    assert dest.is_dir()
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    assert info["path"].endswith("/beelink-foo.ru")
    assert info["url"] == "https://github.com/Garbuzenko/beelink-foo.ru"


# --------------------------------------------------------------------------
# AC #2: refuses to clobber (existing dir OR existing GitHub repo)
# --------------------------------------------------------------------------
def test_refuses_existing_dir(tmp_path: Path):
    proot = tmp_path / "projects"
    proot.mkdir()
    (proot / "beelink-foo").mkdir()       # pre-existing target dir
    proc, proot, log = _run_script(tmp_path, name="foo", projects_root=proot)
    assert proc.returncode != 0
    # No deployer/GitHub side effects: curl never ran.
    assert log.read_text() == ""


def test_refuses_when_github_repo_exists(tmp_path: Path):
    # Existence check returns 200 -> repo exists -> bail before any creation.
    proc, proot, log = _run_script(tmp_path, name="foo", gh_exists_code="200")
    assert proc.returncode != 0
    assert not (proot / "beelink-foo").exists()
    calls = log.read_text()
    # Only the existence GET ran; no deployer POST, no repo-create POST.
    assert "/repos/Garbuzenko/beelink-foo" in calls
    assert "/projects" not in calls
    assert "api.github.com/user/repos" not in calls


# --------------------------------------------------------------------------
# Cleanup trap: a failure AFTER $DEST is created must remove the orphan dir
# (otherwise the Step-1 guard trips forever on retry) and warn about the
# out-of-$DEST side effects already committed. This guards the `|| die` / trap
# interaction: under `set -e` a `cmd || die` does NOT fire an ERR trap, so the
# trap must be on EXIT.
# --------------------------------------------------------------------------
def test_failure_after_dest_created_cleans_up_and_warns(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    # A git that fails on the first call (init), tripping `|| die` at Step 8 — after
    # the deployer registration (Step 3) but before the GitHub repo create (Step 9).
    failing_git = _stub_bin(bindir, "gitfail",
                            'echo "GIT $*" >> "$STUB_LOG"\nexit 1\n')
    proc, proot, log = _run_script(tmp_path, name="foo",
                                   extra_env={"GIT_BIN": str(failing_git)})
    assert proc.returncode != 0
    # The freshly-created orphan dir was removed, so a retry isn't blocked by Step 1.
    assert not (proot / "beelink-foo").exists()
    # The deployer registration happened before the failure -> warn about the orphan.
    assert "deployer project" in proc.stderr
    # No GitHub repo was created yet, so no GitHub warning.
    assert "delete github repo" not in proc.stderr


# --------------------------------------------------------------------------
# AC #3: template excludes .env.example; real gitignored .env written
# --------------------------------------------------------------------------
def test_template_excludes_env_example_and_writes_real_env(tmp_path: Path):
    proc, proot, _ = _run_script(tmp_path, name="foo")
    assert proc.returncode == 0, proc.stderr
    dest = proot / "beelink-foo"
    # No .env.example anywhere in the copied tree.
    assert list(dest.rglob(".env.example")) == []
    env = (dest / ".env").read_text()
    assert "PROJECT_NAME=beelink-foo" in env
    # .env carries the deployer-returned vars too.
    assert "DATABASE_URL=postgres://x" in env
    # .env is matched by .gitignore.
    assert ".env" in (dest / ".gitignore").read_text().splitlines()


def test_env_not_staged_in_initial_commit(tmp_path: Path):
    # Run with REAL git so we can inspect what got committed; still offline (the
    # GitHub create + push curl/git-push are stubbed so nothing leaves the box).
    proc, proot, _ = _run_script(tmp_path, name="foo", real_git=False)
    # With the git stub, init/commit are no-ops; assert via .gitignore instead.
    dest = proot / "beelink-foo"
    assert ".env" in (dest / ".gitignore").read_text()


def test_env_excluded_with_real_git(tmp_path: Path):
    """End-to-end with real git (init/add/commit) but stubbed curl + a git-push
    that is never reached for the network: we replace only push via a wrapper is
    overkill, so we use the git stub for push by checking the committed tree from
    a real git up to commit. Simpler: run real git for init+commit, then assert
    .env is NOT in the committed tree."""
    # Build env where git is real but the GitHub create POST is stubbed (curl) and
    # the push is harmless because origin is an unreachable SSH host — to avoid any
    # network we instead point GIT_BIN at a wrapper that passes through everything
    # except 'push'.
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    git_wrapper = _stub_bin(
        bindir, "gitw",
        'if [ "$1" = "push" ]; then echo "GIT push (stubbed)" >> "$STUB_LOG"; exit 0; fi\n'
        'exec git "$@"\n',
    )
    proc, proot, log = _run_script(
        tmp_path, name="foo", real_git=True,
        extra_env={"GIT_BIN": str(git_wrapper)})
    assert proc.returncode == 0, proc.stderr
    dest = proot / "beelink-foo"
    tracked = subprocess.run(
        ["git", "-C", str(dest), "ls-files"],
        capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_CONFIG_SYSTEM": "/dev/null"}).stdout
    assert ".env" not in tracked.split()
    assert ".env.example" not in tracked


# --------------------------------------------------------------------------
# AC #4: docker-compose container_name + no ports + limit + healthcheck
# --------------------------------------------------------------------------
def test_compose_is_standards_compliant(tmp_path: Path):
    proc, proot, _ = _run_script(tmp_path, name="foo")
    assert proc.returncode == 0, proc.stderr
    compose = (proot / "beelink-foo" / "deploy" / "docker-compose.yml").read_text()
    assert "container_name: beelink-foo" in compose
    # No host ports: a top-level `ports:` mapping must be absent.
    assert "ports:" not in compose
    # Resource limits under deploy.resources.limits (Compose v3 form).
    assert "resources:" in compose and "limits:" in compose
    assert ("memory:" in compose) and ("cpus:" in compose)
    # Healthcheck present.
    assert "healthcheck:" in compose
    # env_file points at the repo-root .env (compose lives under deploy/).
    assert "../.env" in compose
    # Egress-proxy block routes blocked APIs through xray-client.
    assert "xray-client:8080" in compose
    assert "HTTPS_PROXY" in compose and "NO_PROXY" in compose


# --------------------------------------------------------------------------
# AC #5: vision.md AUTOGENERATED header + openspec/ exists
# --------------------------------------------------------------------------
def test_vision_and_openspec_scaffolded(tmp_path: Path):
    proc, proot, _ = _run_script(tmp_path, name="foo", vision="Win the market")
    assert proc.returncode == 0, proc.stderr
    dest = proot / "beelink-foo"
    vfile = dest / "docs" / "strategy" / "vision.md"
    first = vfile.read_text(encoding="utf-8").splitlines()[0]
    assert first == C.VISION_AUTOGEN_HEADER
    assert "Win the market" in vfile.read_text(encoding="utf-8")
    assert (dest / "openspec").is_dir()


# --------------------------------------------------------------------------
# AC #6: GitHub create + push deterministic via API+PAT then SSH (mocked)
# --------------------------------------------------------------------------
def test_github_and_push_are_deterministic(tmp_path: Path):
    proc, proot, log = _run_script(tmp_path, name="foo")
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text()
    # POSTs to the user/repos create endpoint with a Bearer token.
    assert "api.github.com/user/repos" in calls
    assert "Authorization: Bearer ghp_FAKE_TOKEN" in calls
    # Adds the SSH remote and pushes via the git seam (stubbed).
    assert "remote add origin git@github.com:Garbuzenko/beelink-foo.git" in calls
    assert "push -u origin main" in calls
    # The success stdout is the {url, path} JSON.
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    assert info["url"] == "https://github.com/Garbuzenko/beelink-foo"
    assert info["path"].endswith("/beelink-foo")


# --------------------------------------------------------------------------
# create_repo control handler
# --------------------------------------------------------------------------
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


def _mock_orch(tmp_path: Path, script: Path) -> Orchestrator:
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"),
                   create_repo_script=str(script))
    return Orchestrator(cfg)


def _seed_control_row(be: MockBackend, row: int, *, cid="", ts="", action="",
                      args="", status="", result="") -> None:
    be.ensure_control_schema()
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_ID, cid)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_TS, ts)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_ACTION, action)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_ARGS, args)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_STATUS, status)
    be.write_cell(C.CONTROL_TAB, row, C.COL_CTL_RESULT, result)


def _stub_script(tmp_path: Path, *, ok: bool, path: str = "/srv/beelink-foo") -> Path:
    """A stub of the engine that the handler shells out to — never the real one."""
    s = tmp_path / "stub_create.sh"
    if ok:
        body = (
            "#!/usr/bin/env bash\n"
            f'echo \'{{"url": "https://github.com/Garbuzenko/beelink-foo", "path": "{path}"}}\'\n'
            "exit 0\n"
        )
    else:
        body = "#!/usr/bin/env bash\necho 'boom: github repo already exists' >&2\nexit 1\n"
    s.write_text(body)
    s.chmod(s.stat().st_mode | stat.S_IEXEC)
    return s


# AC #7: handler records {url, path}; binds a tab (no sheet Product Vision cell).
def test_create_repo_handler_records_and_binds(tmp_path: Path):
    script = _stub_script(tmp_path, ok=True, path="/srv/beelink-foo")
    orch = _mock_orch(tmp_path, script)
    register_control_handler("create_repo", orch_mod._h_create_repo)
    be = orch.backend
    _seed_control_row(be, 2, cid="c1", ts="2026-06-06 10:00:00Z",
                      action="create_repo",
                      args='{"name": "foo", "vision": "Win SMB auto"}',
                      status=C.CTL_PENDING)

    orch._process_control()

    rows = {r.id: r for r in be.read_control()}
    assert rows["c1"].status == C.CTL_DONE
    assert "https://github.com/Garbuzenko/beelink-foo" in rows["c1"].result
    assert "/srv/beelink-foo" in rows["c1"].result
    # A bound tab now exists (B1 = path); the sheet carries no Product Vision cell.
    tab = be.read_tab("beelink-foo")
    assert tab.repo_binding == "/srv/beelink-foo"
    assert not hasattr(tab, "vision")


# AC: script failure -> error row, daemon survives.
def test_create_repo_handler_script_failure_errors_survives(tmp_path: Path):
    script = _stub_script(tmp_path, ok=False)
    orch = _mock_orch(tmp_path, script)
    register_control_handler("create_repo", orch_mod._h_create_repo)
    be = orch.backend
    _seed_control_row(be, 2, cid="c1", ts="2026-06-06 10:00:00Z",
                      action="create_repo", args='{"name": "foo"}',
                      status=C.CTL_PENDING)

    orch._process_control()   # must NOT raise

    rows = {r.id: r for r in be.read_control()}
    assert rows["c1"].status == C.CTL_ERROR
    assert "create_beelink_repo.sh failed" in rows["c1"].result


def test_create_repo_handler_missing_name_errors(tmp_path: Path):
    script = _stub_script(tmp_path, ok=True)
    orch = _mock_orch(tmp_path, script)
    register_control_handler("create_repo", orch_mod._h_create_repo)
    be = orch.backend
    _seed_control_row(be, 2, cid="c1", ts="2026-06-06 10:00:00Z",
                      action="create_repo", args="{}", status=C.CTL_PENDING)

    orch._process_control()

    rows = {r.id: r for r in be.read_control()}
    assert rows["c1"].status == C.CTL_ERROR
    assert "needs args.name" in rows["c1"].result


# AC #8: one engine, two entry points — the skill shells out to the SAME script.
def test_skill_wrapper_shells_out_to_the_script():
    assert SKILL.exists(), "create-beelink-repo SKILL.md must exist"
    text = SKILL.read_text(encoding="utf-8")
    # It invokes the single engine, not a reimplementation.
    assert "scripts/create_beelink_repo.sh" in text


def test_handler_and_skill_use_same_engine():
    # The daemon default points at the same script the skill names.
    cfg = C.Config(backend="mock")
    assert cfg.create_repo_script.endswith("scripts/create_beelink_repo.sh")
    assert SCRIPT.exists()


def test_script_is_executable():
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR
