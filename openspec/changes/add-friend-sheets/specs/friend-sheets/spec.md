# Spec Delta: friend-sheets

## ADDED Requirements

### Requirement: Friends registry meta-tab

The daemon SHALL maintain a `_friends` meta-tab on the master sheet as the durable
registry of every shared "friend" file. The tab SHALL use a header row 1 and data
from row 2 with the columns `sheet_id | repos | recipient | autonomy | link`,
where `repos` is the newline-or-comma separated repo-allowlist for that file,
`recipient` is the e-mail the file is shared with, `autonomy` is the file's
autonomy level (`gated` when blank), and `link` is the spreadsheet URL. `_friends`
starts with `META_PREFIX` so the repo-dispatch loops never treat it as a repo tab.
The schema SHALL be ensured idempotently (created + header stamped only when
absent), mirroring `_control`, and SHALL be parsed by both the Google and Mock
backends from the identical grid shape.

#### Scenario: Registry tab is bootstrapped idempotently

- **GIVEN** a master sheet with no `_friends` tab
- **WHEN** the daemon ensures the friends schema
- **THEN** a `_friends` tab is created with the header row
  `sheet_id | repos | recipient | autonomy | link`
- **AND** a second ensure call adds no duplicate header and no data rows

#### Scenario: A registry row parses into a Friend record

- **GIVEN** a `_friends` data row `<id> | "repo-a\nrepo-b" | partner@x.com | gated | https://…`
- **WHEN** the daemon reads the friends registry
- **THEN** it yields a Friend with `sheet_id=<id>`, allowlist `{repo-a, repo-b}`,
  `recipient=partner@x.com`, `autonomy=gated` and the link
- **AND** a row with a blank `autonomy` cell yields `autonomy=gated` (the default)
- **AND** a fully blank row is skipped

### Requirement: Per-sheet policy model

A friend sheet SHALL carry a repo allowlist and an autonomy level, exposed as pure
helpers. `friend_repo_allowed(friend, binding)` SHALL return true iff `binding`
matches an entry of the friend's allowlist (compared on the bare repo name and on
the exact binding string). `friend_autonomy(friend, cfg)` SHALL return the
friend's autonomy when set, else `FRIEND_DEFAULT_AUTONOMY`, which itself defaults
to `gated`. The owner MAY raise a specific friend to `ship` by setting that file's
`autonomy` cell.

#### Scenario: Allowlist admits only shared repos

- **GIVEN** a friend with allowlist `{repo-a}`
- **WHEN** the allowlist is checked for binding `repo-a` and for binding `repo-b`
- **THEN** `repo-a` is allowed and `repo-b` is rejected

#### Scenario: Autonomy defaults to gated, owner can raise to ship

- **GIVEN** a friend whose `autonomy` cell is blank
- **WHEN** its effective autonomy is resolved
- **THEN** it is `gated`
- **AND** a friend whose `autonomy` cell is `ship` resolves to `ship`

### Requirement: Share-repos control action

The daemon SHALL register a `share_repos` control handler. Given
`args.recipient` (an e-mail), `args.repos` (a non-empty list of repo bindings,
each already present as a tab on the master sheet) and optional `args.autonomy`,
the handler SHALL: mint a brand-new Google spreadsheet via the Drive API under the
service account; share it with the owner (`OWNER_EMAIL`) and the recipient; seed
it with one bound repo tab per allowlisted repo; append the friend's row to
`_friends`; and write the new file's URL back into the control row result. A
missing `recipient`, an empty `repos`, or a repo not present on the master sheet
SHALL mark the control row `error` without crashing the supervisor. The Drive
mint+share+seed work SHALL sit behind a single seam so the offline test suite can
stub it and never call Google.

#### Scenario: Sharing mints, registers and links a friend file

- **GIVEN** a pending `share_repos` control row with `recipient=partner@x.com` and
  `repos=["repo-a"]` where `repo-a` is bound to a master tab
- **WHEN** the handler runs (with the Drive seam stubbed)
- **THEN** a new spreadsheet is minted and shared with the owner and the recipient
- **AND** a `_friends` row is appended recording the new sheet id, `repo-a`,
  `partner@x.com` and the autonomy
- **AND** the control row is marked `done` with the file URL in the result

#### Scenario: Unknown or empty repo set is rejected

- **GIVEN** a pending `share_repos` row whose `repos` is empty, or names a repo
  with no tab on the master sheet
- **WHEN** the handler runs
- **THEN** the control row is marked `error` with a clear reason
- **AND** no spreadsheet is minted and no `_friends` row is appended

#### Scenario: A friend file cannot create repos (Stage 1 by construction)

- **GIVEN** a freshly minted friend file
- **WHEN** the owner inspects it
- **THEN** it has no bound Apps Script and no `_control` tab, so it cannot enqueue
  any `add_repo` or `create_repo` intent

### Requirement: Share-repos Apps Script entry point

The master sheet's Apps Script SHALL offer a `📤 Поделиться репо` menu item opening
a dialog that collects a recipient e-mail and a multi-select of the master sheet's
repo tabs, and on submit appends ONE `share_repos` intent row to `_control` (id,
ts, action, args JSON, status `pending`), writing only the Apps-Script-owned
columns A–E — never the daemon-owned status/result it later overwrites.

#### Scenario: The dialog enqueues a share_repos intent

- **GIVEN** the owner opens `🤖 Supervisor ▸ 📤 Поделиться репо`, enters a recipient
  and picks one or more repos
- **WHEN** the dialog is submitted
- **THEN** exactly one `_control` row is appended with action `share_repos` and
  args carrying the recipient and the picked repos
- **AND** its status cell is `pending` and the daemon-owned result cell is left blank

### Requirement: Live multi-sheet polling

Each supervisor cycle SHALL poll the master sheet first and then every sheet listed
in the master `_friends` registry, building one backend per friend sheet (cached
across cycles, rebuilt if it errors) and collecting all ready work before
dispatching it together. Every agent write-back — a task row's status/log, the
heartbeat, the chat reply — SHALL be routed to the ORIGINATING sheet's backend, so
a friend-sheet task's state is written to the friend sheet and never to the master.
Each friend-sheet poll SHALL be exception-wrapped so a single unreachable or
malformed friend sheet can never break the cycle or violate the "supervisor never
dies" invariant. A friend-sheet read failure (lost access) or a `_friends` read
failure SHALL skip the affected friend sheet(s) for that cycle, leaving the master
cycle unaffected.

#### Scenario: A cycle dispatches work from both the master and a friend sheet

- **GIVEN** the master sheet has a queued task on `repo-m` and a registered friend
  sheet has a queued task on `repo-f` (in its allowlist)
- **WHEN** one supervisor cycle runs
- **THEN** an agent is dispatched for `repo-m` and an agent for `repo-f`
- **AND** the `repo-m` task's terminal status is written to the master sheet
- **AND** the `repo-f` task's terminal status is written to the friend sheet, whose
  task tab never appears on the master sheet

#### Scenario: A broken friend sheet never breaks the cycle

- **GIVEN** a registered friend sheet whose backend cannot be reached
- **WHEN** a cycle runs
- **THEN** that friend sheet is skipped with a warning
- **AND** the master sheet and every other friend sheet are still polled

### Requirement: Friend-sheet allowlist enforcement

When polling a friend sheet, the daemon SHALL operate ONLY the repos in that file's
`_friends` allowlist. A friend-sheet repo tab (or paired chat tab) whose B1 binding
is outside the allowlist SHALL be refused: its actionable task rows are marked
`blocked` with a clear reason and never dispatched, and a chat question on an
out-of-scope binding is not answered. The allowlist check SHALL run before any repo
resolution or OpenSpec gate (it is the security boundary).

#### Scenario: An out-of-scope tab on a friend sheet is blocked

- **GIVEN** a friend sheet whose allowlist is `{repo-a}` and a repo tab bound to
  `repo-b`
- **WHEN** the daemon collects that tab
- **THEN** no work is dispatched
- **AND** the tab's actionable rows are marked `blocked` on the friend sheet

### Requirement: Per-sheet autonomy on friend sheets

The daemon SHALL apply each friend sheet's effective autonomy (its `_friends`
`autonomy` cell, defaulting to `gated`) to that sheet's tasks, independent of the
master `AUTONOMY`. Under `gated` a friend task runs the SPEC phase and parks for
the owner; under `ship`/`code`/`full` it runs to the corresponding terminal phase.

#### Scenario: A gated friend sheet only specs even when the master ships

- **GIVEN** the master `AUTONOMY` is `ship` and a friend sheet is `gated`
- **WHEN** the daemon plans a friend-sheet task
- **THEN** the task runs the SPEC phase (it does not implement/ship)
- **AND** a friend sheet set to `ship` plans the full implement/ship phase even when
  the master is `gated`

### Requirement: Friend sheets are not a control surface

A friend file SHALL NOT be able to create repos or mint files. The daemon SHALL
process the `_control` intent queue ONLY on the master sheet; it SHALL NOT
bootstrap a `_control` tab on a friend sheet. If a friend sheet already has a
`_control` tab, the daemon SHALL reject any pending `add_repo`, `create_repo` or
`share_repos` intent there (mark it `error`) and ignore (never dispatch) every
other action — so partners can never create repos or mint friend files.

#### Scenario: A repo-creating intent on a friend sheet is rejected

- **GIVEN** a friend sheet with a `_control` tab holding pending `add_repo` and
  `create_repo` intents
- **WHEN** the daemon polls that friend sheet
- **THEN** both intents are marked `error` (“not permitted on a friend sheet”)
- **AND** a non-creating intent on the same tab is left pending, never dispatched

#### Scenario: The daemon never gives a friend sheet a control tab

- **GIVEN** a friend sheet with no `_control` tab
- **WHEN** the daemon polls it
- **THEN** no `_control` tab is created on the friend sheet

### Requirement: Read-only chat on friend sheets

A friend sheet SHALL support the same read-only paired chat as the master: the
daemon SHALL ensure a `_chat <repo>` tab for each allowlisted repo tab ON THE
FRIEND SHEET, consume a pending question from its compose box, and write the
read-only answer back into that friend sheet's transcript and pinned cell — never
the master. Chat on a friend sheet SHALL honour the same allowlist as tasks.

#### Scenario: A question on a friend chat tab is answered on the friend sheet

- **GIVEN** a friend sheet whose chat tab (for an allowlisted repo) has a pending
  question in its compose box
- **WHEN** a cycle runs
- **THEN** the read-only answer is written into that friend sheet's transcript and
  the compose box is reset to the placeholder
- **AND** no chat tab for that question appears on the master sheet
