# Change: add-friend-sheets

## Why

Today the supervisor polls exactly ONE Google Sheet (the owner's master control
plane) and every repo bound to a tab is fully operable from it. To collaborate
with business partners / colleagues the owner needs to hand a partner a *scoped*
view: a separate Google file that exposes only a chosen subset of repos, where
the partner can file tasks but cannot create repos or touch anything outside the
shared set, and whose default autonomy is **gated** (partner files a task → the
agent writes the OpenSpec spec + branch and parks for the owner to review and
deploy). Partners are trusted (same daemon, same Anthropic subscription, no
sandbox), but the blast radius must still be bounded per-sheet.

This is a large feature; it ships as staged OpenSpec tasks. Stage 1 (this change's
implemented slice) lands the durable **`_friends` registry**, the Drive-backed
**share flow** that mints + registers a friend file, the **per-sheet policy model**
(allowlist + autonomy) as pure, tested helpers, and the Apps-Script entry point.
Stage 2 wires the live multi-sheet poll loop + enforcement on top of that
foundation (see `## Non-goals` / `tasks.md` for the staging line).

## What Changes

- **New `_friends` registry meta-tab** on the master sheet (`sheet_id | repos |
  recipient | autonomy | link`): the durable record of every "friend file" the
  owner has minted — which spreadsheet, who it is shared with, which repos are
  allowed in it, and its autonomy level. The sheet stays the single source of
  truth (no local registry that could drift). Seeded/ensured idempotently like
  `_control`.
- **New `share_repos` control action.** The owner picks one or more repos in a
  dialog; the daemon mints a brand-new Google spreadsheet via the Drive API under
  the service account, shares it with the owner (`OWNER_EMAIL`) and the recipient,
  seeds it with a bound repo tab per allowlisted repo, and appends the friend's
  registry row to `_friends`. The Drive/create work sits behind a single seam
  (`Orchestrator._mint_friend_sheet`) so the offline test suite stubs it and never
  touches Google — mirroring how `create_repo` isolates `create_beelink_repo.sh`.
- **Per-sheet policy model** as pure helpers (`friend_repo_allowed`,
  `friend_autonomy`): a friend sheet carries a repo allowlist and an autonomy
  level defaulting to `gated`; the owner may raise a specific friend to `ship`.
- **New config:** `OWNER_EMAIL` (who every minted file is shared back to),
  `FRIEND_DEFAULT_AUTONOMY` (default `gated`), and the Drive OAuth scope added to
  the Google backend's credentials so the SA can create + share files.
- **Apps Script:** a `📤 Поделиться репо` menu item + dialog that enqueues a
  `share_repos` intent (recipient e-mail + picked repos + optional autonomy).

## Non-goals

- **Stage 2 — live multi-sheet polling + enforcement** (the daemon polling every
  registered friend sheet, applying the per-sheet allowlist to reject out-of-scope
  tabs/bindings, applying the per-sheet autonomy, and IGNORING any
  `add_repo`/`create_repo` intent arriving from a friend sheet). The registry,
  policy helpers and share flow this change lands are its foundation; the loop
  refactor (routing every agent write-back to the originating sheet's backend
  without breaking the "supervisor never dies" invariant) is its own staged change.
  A freshly minted friend file has NO bound Apps Script and NO `_control` tab, so
  it cannot enqueue `add_repo`/`create_repo` in the first place — the no-create
  guarantee holds for Stage 1 by construction; Stage 2 enforces it on the loop.
- No sandbox / separate daemon / separate subscription — partners are trusted and
  run on the same supervisor.
- No change to the existing single-sheet invariants: OpenSpec-only gate; the
  supervisor must never die; the sheet is the durable state; the daemon never
  writes a human-owned cell (column A or the B1 binding); chat is read-only.
