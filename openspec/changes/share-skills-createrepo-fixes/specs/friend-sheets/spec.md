# Spec Delta: friend-sheets

## ADDED Requirements

### Requirement: Owner-distributed share (optional recipient)

The share flow SHALL allow minting a friend file with **no named recipient**, so
the owner can carve a repo subset into its own spreadsheet and distribute it
themselves. `Orchestrator.share_repos(recipient, repos, autonomy=None, *,
on_minted=None)` SHALL treat a blank/whitespace recipient as "owner-distributed"
rather than an error, PROVIDED an owner address is configured (`OWNER_EMAIL`); if
the recipient is blank AND no `OWNER_EMAIL` is configured the call SHALL raise
`ValueError` and mint nothing (a file only the service account could reach is
never created). All other validation (≥1 repo, every repo bound on the master
sheet, a valid autonomy) is unchanged.

When the recipient is blank the Drive seam SHALL share the new file back to the
owner (full access) as the only human grant and SHALL NOT perform a
recipient-share, and the appended `_friends` row SHALL record an empty recipient
column. The friend file SHALL NOT be shared with "anyone with the link" — no
unauthenticated/public access is ever granted, because a friend file is a control
plane that dispatches autonomous agents against real repos.

#### Scenario: Mint an owner-distributed file with no recipient

- **GIVEN** a master sheet where `repo-a` is bound to a tab and `OWNER_EMAIL` is
  set
- **WHEN** `share_repos("", ["repo-a"])` runs (Drive seam stubbed)
- **THEN** a new spreadsheet is minted, a `_friends` row recording the new sheet
  id, `repo-a`, a **blank** recipient and the autonomy is appended, and the call
  returns `(sheet_id, url, ["repo-a"], autonomy)`

#### Scenario: Blank recipient with no owner configured is rejected

- **GIVEN** a master sheet with `repo-a` bound but `OWNER_EMAIL` empty
- **WHEN** `share_repos("", ["repo-a"])` runs
- **THEN** it raises `ValueError`, mints no spreadsheet and appends no `_friends`
  row

#### Scenario: Drive seam skips the recipient share when unnamed

- **GIVEN** `OWNER_EMAIL` is set and `_mint_friend_sheet` is invoked with a blank
  recipient
- **THEN** the file is shared back to the owner exactly once and no
  recipient-share call is made, and the file title is derived from the repo
  allowlist (never an empty recipient)

### Requirement: Optional recipient on the share CLI

The `share` CLI subcommand SHALL make `--recipient` **optional**
(`python -m sheet_agent share [--recipient <email>] --repos <a,b>
[--autonomy <level>]`). When `--recipient` is omitted the file is minted and
shared back to the owner (`OWNER_EMAIL`) for the owner to distribute, and the
`_friends` row records a blank recipient. The CLI SHALL continue to reuse the
single `Orchestrator.share_repos` implementation (so the menu and CLI paths never
drift), print the new file's URL and exit zero on success, and report a clear
reason and exit non-zero — minting nothing — on any validation error (empty or
unbound repos, an invalid autonomy, or a blank recipient with no `OWNER_EMAIL`).

#### Scenario: CLI mints an owner-distributed file with no recipient

- **GIVEN** a master sheet where `repo-a` is bound to a tab and `OWNER_EMAIL` is
  set
- **WHEN** `python -m sheet_agent share --repos repo-a` runs (Drive seam stubbed)
- **THEN** a new spreadsheet is minted, a `_friends` row recording the new sheet
  id, `repo-a`, a **blank** recipient and the autonomy is appended, the URL is
  printed, and the command exits zero

#### Scenario: CLI still mints a named-recipient file

- **GIVEN** a master sheet where `repo-a` is bound to a tab
- **WHEN** `python -m sheet_agent share --recipient partner@x.com --repos repo-a`
  runs (Drive seam stubbed)
- **THEN** a new spreadsheet is minted and shared, a `_friends` row recording
  `partner@x.com` is appended, the URL is printed, and the command exits zero
