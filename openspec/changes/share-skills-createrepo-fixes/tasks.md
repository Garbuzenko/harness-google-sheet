# Tasks: share-skills-createrepo-fixes

## 1. Optional recipient in the share flow

- [x] 1.1 `Orchestrator.share_repos`: replace the "recipient required" guard with
  "recipient OR `self.cfg.owner_email` required" — raise `ValueError` only when
  BOTH are blank. A blank recipient otherwise flows through unchanged (normalised
  repos, unbound rejection, autonomy resolution, mint, `on_minted`, `_friends`
  append with the blank recipient).
- [x] 1.2 `_mint_friend_sheet`: when `recipient` is blank, skip the
  recipient-share and derive the file title from the repo allowlist (e.g.
  `beelink • <first-repo> (N repo)`) instead of an empty `beelink •  (N repo)`.
  The owner-share stays best-effort as today.
- [x] 1.3 `__main__`: `share --recipient` is optional (default `""`); update the
  module docstring usage block to `[--recipient <email>]`. The
  `_h_share_repos` result string reads cleanly with a blank recipient.

## 2. Operator-invoked skills-catalog top-up

- [x] 2.1 `sheets`: add `sync_skills()` to the backend (Protocol + `GoogleBackend`
  + `MockBackend`). It ensures the tab, then appends any `DEFAULT_SKILLS` whose
  name is absent from the current grid, leaving existing rows untouched, and
  returns the list of added names.
- [x] 2.2 `__main__`: `skills --sync` calls `sync_skills()` and logs what was
  added; bare `skills` keeps the seed-once behaviour. Update the docstring.

## 3. create_repo accepts dotted names

- [x] 3.1 `scripts/create_beelink_repo.sh`: relax the name regex to
  `^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$` (and update the `die` message + header comment).
- [x] 3.2 `.claude/skills/create-beelink-repo/SKILL.md`: update the documented regex.

## 4. Tests + validate

- [x] 4.1 Friend share (Drive seam stubbed, MockBackend): `share_repos("", [repo])`
  mints + appends a blank-recipient row when `owner_email` is set; raises when
  recipient blank AND `owner_email` empty; `_mint_friend_sheet` skips the
  recipient-share and titles from repos; the `share` CLI happy path with no
  `--recipient`.
- [x] 4.2 Skills sync: `sync_skills()` on an already-seeded tab adds only the
  missing default skills and returns their names; a no-op when nothing is missing;
  it never overwrites a curated row.
- [x] 4.3 create_repo: a dotted bare name (`foo.ru`) runs the script happy path
  to a `{url, path}` JSON; the existing invalid-name variants still fail.
- [x] 4.4 `openspec validate share-skills-createrepo-fixes --strict` passes.
- [x] 4.5 Full offline suite green (`./.venv/bin/python -m pytest -q`).

## 5. Ship + operate

- [x] 5.1 Commit + push to origin on the active branch.
- [x] 5.2 Deploy = make the daemon restart, DEFERRED (the agent is a child of the
  live daemon and must not restart it synchronously) via a `systemd-run --user`
  one-shot timer.
- [~] 5.3 Mint the driving request's file: `share --repos
  $HOME/projects/beelink/beelink-payroll` (owner-distributed).
  BLOCKED: validation passes but minting fails — the Google **Drive API** is not
  enabled for GCP project `example-project-123456` (only the Sheets API is), and the
  service account lacks permission to self-enable it. Enable Drive API in the
  Cloud Console, then re-run the command (or use the menu).
- [x] 5.4 Top up the live catalog: `skills --sync` so `/pagespeed` (and the rest)
  resolve.
