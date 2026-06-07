# Change: polish-for-community

## Why

The task: analyse the repo, remove the junk, review and fix bugs, simplify, and
leave it presentable for a wider audience. A prior `tidy-and-harden` pass already
removed the committed forensics, the duplicate dependency list and the orphan
docs, and fixed the HIGH bugs in the repo-creation engine — that work is on
`main`. But fresh problems remain:

1. **OpenSpec clutter.** Thirteen fully-implemented changes were never archived,
   so `openspec/changes/` held a wall of completed work and `openspec list`
   showed thirteen "pending" items. The `repo-chat` spec had also drifted: the
   `move-chat-to-paired-tab` delta failed to apply (a rename expressed as a
   `MODIFIED` whose header no longer existed), so the published spec still claimed
   chat lives on the repo tab in cols `J/K` after it had moved to a paired tab.

2. **Auto-generated spec purposes.** Every capability spec carried the
   placeholder `## Purpose\nTBD - created by archiving change …` — embarrassing in
   a repo meant to be read by others.

3. **A fresh, adversarially-verified code review** of the post-`tidy-and-harden`
   code (the Russian grid, status colours, paired chat tab, `_skills` catalog,
   repo pruning) to catch correctness bugs and over-complex code introduced since
   the last review.

## What Changes

- **Archive the thirteen completed changes** so `openspec/changes/` holds only
  in-flight work, reconciling `openspec/specs/` with the code already on `main`.
- **Fix the drifted `repo-chat` spec**: express the chat-tab move as a
  `REMOVED` (old `J/K` requirement) + `ADDED` (paired-tab requirement) so the
  published spec matches reality.
- **Replace every `TBD` spec Purpose** with a real one-line statement of what the
  capability is.
- **Apply the verified findings** from the fresh code review (correctness fixes
  and simplifications), each preserving behaviour except where a behaviour was
  itself the bug. Findings that the adversarial verify pass rejected are not
  acted on. The confirmed set:
  - *Correctness (thread safety).* The thread-pool dispatch added two daemon-owned
    fields shared between the pool threads and the main loop — the `_backoff`
    rate-limit flag and the `_ctl_working_since` "entered working" map — written
    under the supervisor lock but read **outside** it. The unlocked `_backoff`
    peek could dispatch a fresh batch onto an already rate-limited API; the
    unlocked `_ctl_working_since` read could wrongly reclaim a freshly dispatched
    skill. Both reads now take the lock (see the `daemon-robustness` delta).
  - *Simplifications.* Drop the dead `self._creds` assignment in `sheets.py`
    (assigned, never read) and correct the stale `TEMPLATE_DIR` doc comment in
    `scripts/create_beelink_repo.sh` (the default is now derived from `--template`).
- **Run `/simplify`** over the resulting diff for reuse/altitude cleanups —
  consolidated the autonomy-level set (`{"spec","code","ship","gated"}`), which was
  duplicated three times across `config.py` and `orchestrator.py`, into a single
  `config.AUTONOMY_LEVELS`/`AUTONOMY_CHOICES` source of truth.

## Non-goals

- No change to the durable-state model (the sheet stays the single source of
  truth) or the invariants (OpenSpec-only gate; supervisor never dies; daemon
  owns sheet cols B/C/D/E/F never A).
- No new sheet columns, runtime dependencies, or changes to the autonomy levels
  or the control-queue contract.
- Local leftover `worktree-wf_*` git branches (workflow-tool debris) are
  unpublished and invisible to the community; pruning them is optional cleanup,
  not a spec'd behaviour.
