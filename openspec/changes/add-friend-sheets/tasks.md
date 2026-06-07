# Tasks: add-friend-sheets

## Stage 1 — registry + share flow + policy model (this run)

### 1. Config
- [x] 1.1 Add `OWNER_EMAIL` and `FRIEND_DEFAULT_AUTONOMY` (default `gated`) to
  `Config`, validated against `spec|code|ship|gated`.
- [x] 1.2 Add the `_friends` layout constants (`FRIENDS_TAB`, header, 1-indexed
  columns) and the Drive OAuth scope to the Google backend credentials.

### 2. `_friends` registry (both backends)
- [x] 2.1 `Friend` dataclass + `parse_friends_grid` (allowlist split on newline/comma,
  `gated` default for a blank autonomy cell, blank rows skipped).
- [x] 2.2 `ensure_friends_schema` + `read_friends` on `GoogleBackend` and
  `MockBackend`, idempotent + quota-friendly like `_control`.

### 3. Per-sheet policy model (pure helpers)
- [x] 3.1 `friend_repo_allowed(friend, binding)` and `friend_autonomy(friend, cfg)`.

### 4. `share_repos` control action
- [x] 4.1 `Orchestrator._mint_friend_sheet(...)` Drive seam: create spreadsheet,
  share with owner + recipient, seed allowlisted repo tabs. Stubbed in tests.
- [x] 4.2 `_h_share_repos` handler: validate recipient + repos (each bound on the
  master sheet), mint, register the `_friends` row, write the URL to the control
  result; reject empty/unknown repos as `error` without crashing.

### 5. Apps Script
- [x] 5.1 `📤 Поделиться репо` menu item + dialog + `enqueueShareRepos` →
  `share_repos` intent (recipient + picked repos + optional autonomy).

### 6. Tests + validate + ship
- [x] 6.1 Tests: registry parse/ensure (both backends), policy helpers, the
  `share_repos` handler with a stubbed Drive seam (success + rejection paths),
  Apps-Script intent shape.
- [x] 6.2 `openspec validate add-friend-sheets --strict` passes.
- [x] 6.3 Full offline suite green (`./.venv/bin/python -m pytest -q`).
- [x] 6.4 Commit, push to origin, deploy (deferred daemon restart — the agent is a
  child of the live daemon and must not restart it synchronously).

## Stage 2 — live multi-sheet polling + enforcement (next run)

- [x] 7.1 Build a backend per registered friend sheet each cycle from `_friends`
  (`make_friend_backend` + cached `_friend_backends`); route every agent write-back
  to the originating sheet's backend (carry a `SheetCtx` on `WorkItem`/`ChatWork`)
  so a friend-sheet task's status is written to the friend sheet, never the master.
- [x] 7.2 Extend `run_once` to poll the master sheet, then each friend sheet
  (`_poll_sheet` per `SheetCtx`), all exception-wrapped so one bad friend sheet can
  never break the cycle or the "supervisor never dies" invariant.
- [x] 7.3 Enforce the per-sheet allowlist: a friend-sheet repo/chat tab whose binding
  is outside that file's allowlist is marked `blocked` / refused, never dispatched.
- [x] 7.4 Apply each friend sheet's `friend_autonomy` to its tasks (gated by
  default), independent of the master `AUTONOMY`.
- [x] 7.5 The `_control` queue is processed ONLY on the master; a friend `_control`
  tab is never bootstrapped, and a pending `add_repo`/`create_repo`/`share_repos`
  there is rejected (`error`), so partners can never create repos or mint files.
- [x] 7.6 Tests for the multi-sheet loop, allowlist rejection, per-sheet autonomy,
  the create-rejection guard, and friend-sheet chat (`tests/test_friend_sheets_loop.py`);
  suite green; redeploy.
