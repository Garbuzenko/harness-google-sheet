"""Offline tests for repo.py resolution + publish seams that test_core.py leaves
uncovered: the git-URL branch of resolve() and the git-publish seam. No network —
git is monkeypatched at the single subprocess.run seam in repo.py."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sheet_agent import config as C
from sheet_agent import repo as R
from sheet_agent.repo import _looks_like_git_url, _name_from_url, publish, resolve


def test_looks_like_git_url():
    assert _looks_like_git_url("git@github.com:me/x.git")
    assert _looks_like_git_url("https://github.com/me/x.git")
    assert _looks_like_git_url("ssh://git@host/x")
    assert _looks_like_git_url("https://host/x")          # http(s):// even without .git
    assert not _looks_like_git_url("/abs/path")
    assert not _looks_like_git_url("bare-name")


def test_name_from_url():
    assert _name_from_url("git@github.com:me/myrepo.git") == "myrepo"
    assert _name_from_url("https://github.com/me/myrepo") == "myrepo"
    assert _name_from_url("https://github.com/me/myrepo/") == "myrepo"


def test_resolve_git_url_reuses_existing_clone(tmp_path: Path):
    clone_root = tmp_path / "clones"
    (clone_root / "myrepo" / ".git").mkdir(parents=True)
    cfg = C.Config(clone_root=str(clone_root))
    r = resolve("git@github.com:me/myrepo.git", cfg)
    assert r.ok and r.path == (clone_root / "myrepo").resolve()


def test_resolve_git_url_refuses_same_named_non_git_dir(tmp_path: Path):
    # A bare same-named dir (typo / stale scratch) is NOT the repo the URL points
    # at; trusting it would run agents in the wrong working tree.
    clone_root = tmp_path / "clones"
    (clone_root / "myrepo").mkdir(parents=True)            # exists, but no .git
    cfg = C.Config(clone_root=str(clone_root))
    r = resolve("git@github.com:me/myrepo.git", cfg)
    assert not r.ok and "not a git repo" in r.reason


def test_resolve_git_url_clones_when_missing(tmp_path: Path, monkeypatch):
    clone_root = tmp_path / "clones"
    cfg = C.Config(clone_root=str(clone_root))
    dest = clone_root / "myrepo"

    def fake_run(args, **kw):
        # emulate `git clone URL dest` by materialising the checkout
        assert args[0] == "git" and args[1] == "clone"
        Path(args[-1] + "/.git").mkdir(parents=True)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(R.subprocess, "run", fake_run)
    r = resolve("git@github.com:me/myrepo.git", cfg)
    assert r.ok and r.path == dest.resolve() and (dest / ".git").is_dir()


def test_resolve_git_url_clone_failure_is_reported(tmp_path: Path, monkeypatch):
    cfg = C.Config(clone_root=str(tmp_path / "clones"))

    def boom(args, **kw):
        raise subprocess.CalledProcessError(128, args, "", "fatal: repo not found")

    monkeypatch.setattr(R.subprocess, "run", boom)
    r = resolve("git@github.com:me/missing.git", cfg)
    assert not r.ok and "git clone failed" in r.reason


def test_discover_skips_junk_dirs_and_honours_max_depth(tmp_path: Path):
    # node_modules is junk we never descend into, even if it holds a ".git".
    (tmp_path / "node_modules" / "pkg" / ".git").mkdir(parents=True)
    # A real repo just inside the depth budget (ROOT/a/b/c/repo == depth 4).
    (tmp_path / "a" / "b" / "c" / "repo" / ".git").mkdir(parents=True)
    # One level too deep — pruned by _MAX_DEPTH.
    (tmp_path / "a" / "b" / "c" / "d" / "toodeep" / ".git").mkdir(parents=True)
    cfg = C.Config(repo_search_roots=(str(tmp_path),))
    names = {r.name for r in R.discover(cfg)}
    assert names == {"a/b/c/repo"}


def test_publish_stages_commits_and_pushes(tmp_path: Path, monkeypatch):
    calls = []

    def rec(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(R.subprocess, "run", rec)
    publish(tmp_path, ["openspec/changes/x/proposal.md"], "feat: x")
    verbs = [a[3] if a[1] == "-C" else a[1] for a in calls]
    assert verbs == ["add", "commit", "push"]


def test_publish_tolerates_empty_commit_but_still_pushes(tmp_path: Path, monkeypatch):
    # "nothing to commit" must NOT abort — a prior commit may still need pushing.
    pushed = {"yes": False}

    def rec(args, **kw):
        if "commit" in args:
            return subprocess.CompletedProcess(args, 1, "nothing to commit, working tree clean", "")
        if "push" in args:
            pushed["yes"] = True
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(R.subprocess, "run", rec)
    publish(tmp_path, ["f"], "noop")          # must not raise
    assert pushed["yes"]


def test_publish_raises_on_real_commit_error(tmp_path: Path, monkeypatch):
    def rec(args, **kw):
        if "commit" in args:
            return subprocess.CompletedProcess(args, 1, "", "fatal: bad object")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(R.subprocess, "run", rec)
    with pytest.raises(subprocess.CalledProcessError):
        publish(tmp_path, ["f"], "msg")
