"""Resolve a tab's repo binding to a local working directory.

Binding (cell B1) may be:
  * an absolute path            -> used as-is
  * a git URL (ssh or https)    -> cloned into CLONE_ROOT if missing
  * a bare name                 -> searched under REPO_SEARCH_ROOTS
"""
from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config as C
from .log import log


@dataclass
class RepoResult:
    ok: bool
    path: Path | None = None
    reason: str = ""


@dataclass
class RepoInfo:
    name: str
    path: Path
    has_openspec: bool


# Junk/archive we never descend into while hunting for git checkouts.
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
              ".next", "dist", "build", ".cache", "target", "vendor",
              "trash", "archive", "_archive", "old", ".trash"}
_MAX_DEPTH = 4   # ROOT/repo … ROOT/cat/sub/sub/repo (e.g. auto/channels/b2c/acme-ai)


def discover(cfg: C.Config) -> list[RepoInfo]:
    """Find selectable repos: git checkouts under REPO_SEARCH_ROOTS, including
    ones nested in category folders (e.g. platform/auto/dealer or the deeper
    platform/auto/channels/b2c/acme-ai). The repo's name is its path relative to
    the root (`auto/channels/b2c/acme-ai`) — which `resolve()` binds via its
    bare-name branch. First root wins on name clashes; we never descend into a
    repo (no submodules) or into junk dirs.

    A non-git directory is always a category folder we descend INTO to find the
    real checkouts nested under it — a `.gitignore` there does not make it a repo
    (e.g. `beelink-example.ru/` is a category folder holding the real
    `example.ru` and `wish.example.ru` checkouts). To prune a genuinely
    неактуальный repo from the add-repo dropdown, match it with a `REPO_IGNORE`
    glob — never by guessing from a `.gitignore`.
    """
    ignore = cfg.repo_ignore

    def ignored(rel: str) -> bool:
        return any(fnmatch.fnmatch(rel, pat) for pat in ignore)

    found: dict[str, RepoInfo] = {}

    def walk(rp: Path, d: Path, depth: int) -> None:
        if depth > _MAX_DEPTH:
            return
        try:
            children = sorted((c for c in d.iterdir() if c.is_dir()),
                              key=lambda p: p.name.lower())
        except OSError:
            return
        for c in children:
            if c.name.startswith(".") or c.name in _SKIP_DIRS:
                continue
            if (c / ".git").exists():               # dir or file (worktrees)
                rel = c.relative_to(rp).as_posix()
                if not ignored(rel):
                    found.setdefault(rel, RepoInfo(rel, c, (c / "openspec").is_dir()))
                continue                            # don't descend into a repo
            walk(rp, c, depth + 1)                  # category folder — descend to find checkouts

    for root in cfg.repo_search_roots:
        rp = Path(root).expanduser()
        if rp.is_dir():
            walk(rp, rp, 1)
    return list(found.values())


def _looks_like_git_url(s: str) -> bool:
    return (
        s.startswith("git@")
        or s.endswith(".git")
        or s.startswith("ssh://")
        or (s.startswith("http") and "://" in s)
    )


def _name_from_url(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def _ok(p: Path) -> RepoResult:
    """A successful resolution, with the working dir CANONICALISED (`resolve()`:
    absolute, symlinks + `..` collapsed). This is load-bearing: the orchestrator's
    per-repo serialisation keys `_inflight` on this path, and two tabs may bind the
    SAME physical repo via different forms (abs path / bare name / trailing slash /
    symlink). Without one canonical key the lock is bypassed and two agents run in
    one working tree — a git index/checkout/commit race. Canonicalise at the single
    seam that produces the dir so every consumer shares the same key."""
    return RepoResult(True, p.resolve())


def resolve(binding: str, cfg: C.Config) -> RepoResult:
    binding = (binding or "").strip()
    if not binding:
        return RepoResult(False, reason="empty REPO_PATH (fill cell B1 with a path or git url)")

    # Absolute path
    if binding.startswith("/") or binding.startswith("~"):
        p = Path(binding).expanduser()
        if p.is_dir():
            return _ok(p)
        return RepoResult(False, reason=f"path does not exist: {p}")

    # Git URL -> clone if needed
    if _looks_like_git_url(binding):
        name = _name_from_url(binding)
        dest = Path(cfg.clone_root).expanduser() / name
        if dest.is_dir():
            # Reuse a prior clone — but only if it's actually a git repo. A bare
            # same-named dir (a typo, a stale scratch folder) is NOT the repo this
            # URL points at; trusting it would run agents in the wrong tree.
            if (dest / ".git").exists():
                return _ok(dest)
            return RepoResult(False, reason=(
                f"{dest} exists but is not a git repo (no .git); "
                f"refusing to treat it as {binding}"))
        log.info("cloning %s -> %s", binding, dest)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--depth", "50", binding, str(dest)],
                check=True, capture_output=True, text=True, timeout=600,
            )
            return _ok(dest)
        except subprocess.CalledProcessError as e:
            return RepoResult(False, reason=f"git clone failed: {e.stderr.strip()[:300]}")
        except subprocess.TimeoutExpired:
            return RepoResult(False, reason="git clone timed out")

    # Bare name -> search known roots
    for root in cfg.repo_search_roots:
        cand = Path(root).expanduser() / binding
        if cand.is_dir():
            return _ok(cand)
    return RepoResult(
        False,
        reason=f"repo {binding!r} not found in roots: {', '.join(cfg.repo_search_roots)}",
    )


def has_openspec(path: Path) -> bool:
    return (path / "openspec").is_dir()


def publish(repo_path: Path, rel_paths: list[str], message: str) -> None:
    """Stage `rel_paths`, commit with `message`, and push to `origin` over SSH.

    The single git-publish seam (git operations belong in repo.py): the daemon
    never shells out to git inline. Tests monkeypatch `subprocess.run` here so no
    live network call ever happens offline. A clean tree ("nothing to commit") is
    tolerated — we still push so a prior commit reaches origin — but a failing
    push (no network, auth) raises so the caller can mark the row `error`."""
    repo_path = Path(repo_path)
    subprocess.run(
        ["git", "-C", str(repo_path), "add", *rel_paths],
        check=True, capture_output=True, text=True, timeout=120,
    )
    commit = subprocess.run(
        ["git", "-C", str(repo_path), "commit", "-m", message],
        capture_output=True, text=True, timeout=120,
    )
    if commit.returncode != 0:
        out = (commit.stdout or "") + (commit.stderr or "")
        # A no-op commit (nothing changed) is fine; anything else is a real error.
        if "nothing to commit" not in out and "no changes added" not in out:
            raise subprocess.CalledProcessError(
                commit.returncode, commit.args, commit.stdout, commit.stderr
            )
    subprocess.run(
        ["git", "-C", str(repo_path), "push", "origin", "HEAD"],
        check=True, capture_output=True, text=True, timeout=300,
    )
