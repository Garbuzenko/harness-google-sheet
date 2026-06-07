"""CLI entrypoint.

  python -m sheet_agent run        # supervisor loop (default)
  python -m sheet_agent once       # one poll cycle, then exit
  python -m sheet_agent doctor     # validate config + sheet connectivity
  python -m sheet_agent bootstrap  # ensure schema + formatting on every tab
  python -m sheet_agent repos      # (re)build the _repos reference tab + B1 dropdowns
  python -m sheet_agent skills     # create + seed the _skills catalog tab (seed-once)
  python -m sheet_agent skills --sync  # top up an existing catalog with newly-
                                   # available default skills (never clobbers edits)
  python -m sheet_agent share [--recipient <email>] --repos a,b [--autonomy gated]
                                   # mint + share a friend file head-less (no menu);
                                   # no --recipient = owner-distributed (share yourself)
"""
from __future__ import annotations

import argparse
import sys

from . import config as C
from . import repo as repolib
from .log import log
from .orchestrator import Orchestrator, run as run_loop


def _doctor(cfg: C.Config) -> int:
    problems = cfg.validate()
    if problems:
        for p in problems:
            log.error("config: %s", p)
        return 1
    log.info("config OK (backend=%s autonomy=%s model=%s)",
             cfg.backend, cfg.autonomy, cfg.model)
    try:
        be = Orchestrator(cfg).backend
        titles = be.list_tab_titles()
        log.info("connected. tabs (%d): %s", len(titles), titles)
        for t in titles:
            # Meta tabs (`_repos`, `_control`) are NOT repo tabs and must never get
            # the repo grid stamped on them. `read_tab` bootstraps the repo schema on
            # first read, so calling it on `_control`/`_repos` corrupts their header
            # (REPO_PATH/Task clobbers id|ts|action|... and the repos list).
            # Every other tab loop skips META_PREFIX — doctor must too.
            if t.startswith(C.META_PREFIX):
                log.info("  %-20s (meta — schema not stamped)", t)
                continue
            tab = be.read_tab(t)
            log.info("  %-20s repo=%r rows=%d", t, tab.repo_binding, len(tab.rows))
    except Exception as e:  # noqa: BLE001
        log.error("connectivity check failed: %s", e)
        return 2
    return 0


def _bootstrap(cfg: C.Config) -> int:
    be = Orchestrator(cfg).backend
    _repos(cfg, be)  # build _repos first so the B1 dropdown has a source
    _skills(cfg, be)  # seed the _skills catalog (only if absent — never clobbers edits)
    for t in be.list_tab_titles():
        if t.startswith(C.CHAT_TAB_PREFIX):
            be.ensure_chat_schema(t)  # paired chat tab: stamp its own (non-task) schema
            log.info("chat schema ensured on %r", t)
            continue
        if t.startswith(C.META_PREFIX):
            continue
        be.ensure_schema(t)
        be.prettify(t)  # (re)apply formatting; migrate old layouts to the A–F grid
        log.info("schema ensured + prettified on %r", t)
    return 0


def _repos(cfg: C.Config, be=None) -> int:
    be = be or Orchestrator(cfg).backend
    repos = repolib.discover(cfg)
    be.ensure_repos_tab(repos)
    log.info("built %s with %d repos: %s", C.REPOS_TAB, len(repos),
             ", ".join(r.name for r in repos) or "(none found)")
    for t in be.list_tab_titles():
        if t.startswith(C.META_PREFIX):
            continue
        be.set_repo_dropdown(t)
        log.info("repo dropdown set on %r", t)
    return 0


def _skills(cfg: C.Config, be=None, sync: bool = False) -> int:
    """Create + seed the `_skills` catalog tab. Seed runs only when the tab is
    absent/empty, so an operator's prunes and prompt edits are never clobbered.
    With `sync=True`, additionally top up an already-seeded catalog with any
    newly-available default skills (appended; existing rows left untouched)."""
    be = be or Orchestrator(cfg).backend
    be.ensure_skills_tab()
    if sync:
        added = be.sync_skills()
        log.info("synced %s: +%d skill(s): %s", C.SKILLS_TAB, len(added),
                 ", ".join(added) or "(nothing missing)")
    skills = be.read_skills()
    log.info("ensured %s with %d skills: %s", C.SKILLS_TAB, len(skills),
             ", ".join(s.name for s in skills) or "(none)")
    return 0


def _share(cfg: C.Config, recipient: str, repos: list[str],
           autonomy: str | None) -> int:
    """Mint + share a friend file head-less, reusing the SAME validated flow as the
    `share_repos` control handler (`Orchestrator.share_repos`). Prints the new file
    URL on success; reports the reason and returns non-zero on a validation error."""
    problems = cfg.validate()
    if problems:
        for p in problems:
            log.error("config: %s", p)
        log.error("refusing to share; fix config (see README).")
        return 1
    orch = Orchestrator(cfg)  # like doctor/repos: no daemon loop, no instance lock
    try:
        _sid, url, repos, autonomy = orch.share_repos(recipient, repos, autonomy)
    except ValueError as e:
        log.error("share failed: %s", e)
        return 2
    log.info("shared %d repo(s) with %s [%s]: %s",
             len(repos), recipient, autonomy, url)
    print(url)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sheet-agent")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "once", "doctor", "bootstrap", "repos",
                                 "skills", "share"])
    parser.add_argument("--recipient", help="share: e-mail to share the friend file "
                        "with (optional — omit to mint an owner-distributed file)")
    parser.add_argument("--repos", help="share: comma/newline-separated repo bindings")
    parser.add_argument("--autonomy", help="share: friend autonomy (spec|code|ship|gated)")
    parser.add_argument("--sync", action="store_true",
                        help="skills: top up the catalog with newly-available default "
                        "skills (appended; existing rows never clobbered)")
    args = parser.parse_args(argv)
    cfg = C.load()

    if args.command == "share":
        return _share(cfg, args.recipient or "",
                      args.repos or "", args.autonomy)

    if args.command == "doctor":
        return _doctor(cfg)
    if args.command == "bootstrap":
        return _bootstrap(cfg)
    if args.command == "repos":
        return _repos(cfg)
    if args.command == "skills":
        return _skills(cfg, sync=args.sync)

    if args.command == "once":
        problems = cfg.validate()
        if problems:
            for p in problems:
                log.error("config: %s", p)
            log.error("refusing to run once; fix config (see README).")
            return 1
        # One cycle through the SAME code path as the loop (`run_once`), draining the
        # background agent + chat pools before exit so a one-shot run never abandons
        # dispatched work.
        Orchestrator(cfg).run_once(drain=True)
        return 0

    # `run`: degraded-wait startup — never crash-loop on missing creds.
    run_loop(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
