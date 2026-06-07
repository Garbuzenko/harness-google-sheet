# add-existing-repo Specification

## Purpose
How a human binds an existing git repo to a sheet tab and how the daemon provisions that tab.
## Requirements
### Requirement: add_repo control handler

The daemon SHALL register an `add_repo` handler in the control-action registry.
Given `args.path` (the path of an existing repo), the handler SHALL provision a NEW
repo-bound sheet tab whose title is the sanitized LAST path segment, bind it
(B1 = path), and bootstrap it to the current grid layout. The handler SHALL NOT seed
any Product Vision cell (there is no Vision row). The handler SHALL be idempotent on
the bound path, SHALL never write a human-owned cell of any OTHER existing tab, and a
missing `args.path` SHALL mark the control row `error` without crashing the supervisor.

#### Scenario: New bound tab created from a path

- **GIVEN** a pending `add_repo` control row with `args.path` set to a repo path
  whose last segment contains Google-Sheets-illegal chars (e.g. `/a/b/c:d?`)
- **WHEN** the handler runs
- **THEN** a new tab is created whose title is the LAST path segment with the illegal
  chars `: \ / ? * [ ]` stripped and the result capped at 100 chars (e.g. `cd`)
- **AND** the new tab's B1 equals the given path
- **AND** the new tab is bootstrapped: A1 reads `REPO_PATH` and the header row (row 2)
  carries the standard task columns
- **AND** the control row is marked `done`

#### Scenario: Title collision is suffixed -2, then -3

- **GIVEN** a tab already exists with the target sanitized title (bound to a
  different path)
- **WHEN** `add_repo` runs for a path that sanitizes to the same base title
- **THEN** the new tab is created with the title suffixed `-2`
- **AND** a further `add_repo` for yet another path with the same base yields `-3`
- **AND** every produced title stays within the 100-char Sheets limit

#### Scenario: Idempotent no-op for an already-bound path

- **GIVEN** a tab already bound (B1) to the EXACT path in `args.path`
- **WHEN** `add_repo` runs again for that same path
- **THEN** no new tab is created (the tab count is unchanged)
- **AND** the control row is marked `done` (NOT `error`) with a result noting the
  repo is already bound

#### Scenario: Never writes another tab's human-owned cells

- **GIVEN** an existing repo tab with a human-owned Task (A) and a B1 binding
- **WHEN** `add_repo` provisions a different new tab
- **THEN** the existing tab's A cell and its B1 binding are byte-for-byte unchanged
- **AND** only the NEW tab's B1 and schema labels are written

#### Scenario: Missing path marks error and the daemon survives

- **GIVEN** a pending `add_repo` control row with no `args.path`
- **WHEN** the handler runs in a dispatch cycle
- **THEN** the control row is marked `error`
- **AND** no exception propagates out of the supervisor loop

### Requirement: create_tab backend parity

Both `GoogleBackend` and `MockBackend` SHALL expose a behaviourally-equivalent
`create_tab(title)` that creates an empty worksheet/tab if it does not exist and is
a no-op (no duplicate, existing grid untouched) if it does. Handlers SHALL use this
seam rather than reaching into gspread directly.

#### Scenario: create_tab is idempotent on both backends

- **WHEN** `create_tab(title)` is called for a title that does not yet exist
- **THEN** an empty tab with that title exists
- **AND** a second `create_tab(title)` for the same title adds no duplicate and
  leaves any existing grid contents untouched

