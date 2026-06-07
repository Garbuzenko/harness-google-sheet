# Spec Delta: friend-sheets

## ADDED Requirements

### Requirement: Headless share-repos CLI

The package SHALL expose a `share` CLI subcommand
(`python -m sheet_agent share --recipient <email> --repos <a,b>
[--autonomy <level>]`) that mints + shares a friend file head-less, without the
Apps-Script menu and without running the supervisor loop. The command SHALL reuse
the SAME validated share implementation as the `share_repos` control handler — a
single `Orchestrator.share_repos` method that validates the recipient, normalises
the repo list, rejects any repo not bound on the master sheet, resolves and
validates the autonomy (defaulting to `FRIEND_DEFAULT_AUTONOMY`), mints the file
via the existing Drive seam, and appends the `_friends` registry row — so the menu
path and the CLI path can never drift. On success the command SHALL print the new
file's URL and exit zero; on a validation error (missing recipient, empty or
unbound repos, or an invalid autonomy) it SHALL report a clear reason and exit
non-zero, minting nothing. Constructing the orchestrator for the command SHALL NOT
take the daemon's single-instance lock, mirroring the other read/admin
subcommands.

#### Scenario: CLI mints and shares a friend file

- **GIVEN** a master sheet where `repo-a` is bound to a tab
- **WHEN** `python -m sheet_agent share --recipient partner@x.com --repos repo-a`
  runs (with the Drive seam stubbed)
- **THEN** a new spreadsheet is minted and shared, a `_friends` row recording the
  new sheet id, `repo-a`, `partner@x.com` and the autonomy is appended, the file
  URL is printed, and the command exits zero

#### Scenario: CLI rejects an unbound or empty repo set

- **GIVEN** a `share` invocation whose `--repos` is empty or names a repo with no
  tab on the master sheet
- **WHEN** the command runs
- **THEN** it prints a clear reason, exits non-zero, mints no spreadsheet and
  appends no `_friends` row

### Requirement: Shared share-repos implementation

The orchestrator SHALL hold the share flow in one method,
`Orchestrator.share_repos(recipient, repos, autonomy=None, *, on_minted=None)`,
returning `(sheet_id, url, repos, autonomy)`. It SHALL validate the recipient is
non-empty, accept `repos` as either a separator-delimited string or a list,
reject any repo not bound on the master sheet, resolve a blank autonomy to
`FRIEND_DEFAULT_AUTONOMY` and reject an autonomy outside the allowed levels, mint
via the Drive seam, then append the `_friends` registry row. When provided,
`on_minted(sheet_id, url)` SHALL be invoked AFTER the mint and BEFORE the registry
append, so a caller can durably record the minted URL before the registry write.
The `share_repos` control handler SHALL delegate to this method, supplying an
`on_minted` callback that writes the minted URL into the control row result.

#### Scenario: on_minted fires between mint and registry write

- **GIVEN** a stubbed Drive seam that returns a known `(sheet_id, url)`
- **WHEN** `share_repos` is called with an `on_minted` callback
- **THEN** the callback is invoked exactly once with that `(sheet_id, url)` before
  the `_friends` row is appended

#### Scenario: Control handler still records the minted URL

- **GIVEN** a pending `share_repos` control row for a bound repo
- **WHEN** the handler runs (Drive seam stubbed)
- **THEN** the control result records the minted URL and a `_friends` row is
  appended, exactly as before the refactor
