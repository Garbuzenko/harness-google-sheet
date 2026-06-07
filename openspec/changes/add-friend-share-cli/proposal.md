# Change: add-friend-share-cli

## Why

The `share_repos` flow (Stage 1 of `add-friend-sheets`) mints + shares a scoped
"friend" spreadsheet for a chosen subset of repos. Today it can ONLY be triggered
from the master sheet's Apps Script menu (`📤 Поделиться репо` → a `_control`
intent). There is no headless entry point, so an operator on the server shell —
or an autonomous agent — cannot mint a friend file without opening the Sheet UI
in a browser and clicking a menu.

The immediate driver: the owner wants to carve `wish.example.ru` and
`example.ru` into their own shared spreadsheet for
`partner@example.com`. That is exactly one `share_repos` invocation, but
there is no command to run it head-less. A `share` CLI subcommand closes that gap
and reuses the SAME validated mint/register logic the control handler uses, so the
two entry points can never drift.

## What Changes

- **New `Orchestrator.share_repos(recipient, repos, autonomy=None, *, on_minted=None)`
  method** that holds the single, shared implementation of the share flow:
  validate the recipient, normalise the repo list (string or list), reject any
  repo not bound on the master sheet, resolve + validate the autonomy, mint the
  file via the existing Drive seam (`_mint_friend_sheet`), then append the
  `_friends` registry row. It returns `(sheet_id, url, repos, autonomy)`. An
  optional `on_minted(sheet_id, url)` callback fires AFTER the mint but BEFORE the
  registry write, preserving the handler's "record the URL even if the registry
  write later hiccups" ordering.
- **`_h_share_repos` refactored** to delegate to `share_repos`, passing an
  `on_minted` callback that writes `minted <url>` into the control result. Same
  behaviour, no duplicated validation.
- **New `share` CLI subcommand** in `__main__.py`:
  `python -m sheet_agent share --recipient <email> --repos <a,b> [--autonomy gated]`.
  It validates config, constructs the `Orchestrator` (no daemon loop, no
  single-instance lock — like `doctor`/`repos`), calls `share_repos`, logs +
  prints the new file URL, and returns non-zero with a clear message on a
  validation error (missing recipient / empty or unbound repos / bad autonomy).

## Non-goals

- No change to the Drive seam, the `_friends` registry schema, the policy helpers,
  or the Apps-Script menu — those all stay as Stage 1 landed them.
- No Stage 2 work (live multi-sheet polling + per-sheet enforcement) — still its
  own change.
- No new sharing semantics: the CLI is purely a second trigger for the existing,
  already-tested share flow.
- No change to the single-sheet invariants: OpenSpec-only gate; the supervisor
  must never die; the sheet is the durable state; the daemon never writes a
  human-owned cell; chat is read-only.
