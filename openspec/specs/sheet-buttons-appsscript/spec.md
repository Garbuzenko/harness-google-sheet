# sheet-buttons-appsscript Specification

## Purpose
The Apps Script custom menu and dialogs that let a human drive the daemon from the sheet.
## Requirements
### Requirement: Supervisor custom menu

The Apps Script layer SHALL build, on `onOpen(e)`, a custom menu titled exactly
`🤖 Supervisor` via `SpreadsheetApp.getUi().createMenu` with EXACTLY three items
wired to the three dialog handler functions: add repo, create repo, and run skill.
The Apps Script source SHALL live in `appsscript/` in git (the source of truth)
alongside its clasp config — `appsscript.json` (manifest) and `.clasp.json`.
Pushing to Google via `clasp push` requires interactive OAuth and is a separate
human step, out of scope for this change.

#### Scenario: onOpen builds the Supervisor menu

- **GIVEN** the spreadsheet is opened
- **WHEN** `onOpen(e)` runs
- **THEN** a custom menu titled exactly `🤖 Supervisor` is created via
  `SpreadsheetApp.getUi().createMenu`
- **AND** it has EXACTLY three `addItem` entries bound to the add-repo, create-repo
  and run-skill dialog handler functions
- **AND** the menu is added to the UI (`addToUi`)

#### Scenario: clasp project committed to git

- **GIVEN** the repository
- **WHEN** the `appsscript/` directory is inspected
- **THEN** it contains the Apps Script source (`Code.gs`), the manifest
  `appsscript.json`, and the clasp config `.clasp.json`
- **AND** all are committed to git (git is the source of truth)

### Requirement: Dialogs append only to the control queue

Every Supervisor dialog SHALL append to the `_control` tab ONLY — never to a repo
tab's human-owned cells (A/D/H) or its B1 binding, and never to the server. Each
appended row SHALL set ONLY columns A-E: A=`id` (`<ts>-<rand>`), B=`ts` (ISO time),
C=`action`, D=`args` (JSON), E=`status`=`pending`, leaving F (`result`,
daemon-owned) blank.

#### Scenario: Append writes only A-E with status pending

- **GIVEN** any Supervisor dialog is confirmed
- **WHEN** it appends a row to `_control`
- **THEN** the row sets `id` (`<ts>-<rand>`), `ts` (ISO), `action`, `args` (JSON),
  and `status` = `pending`
- **AND** it never sets the daemon-owned `result` (column F)
- **AND** it targets the `_control` tab and no repo tab's A/D/H/B1

### Requirement: Add-repo dialog

The add-repo dialog SHALL read `_repos!A2:C`, let the human select repos, and append
ONE `_control` row per selected repo with `action="add_repo"` and an `args` JSON
object containing `repo` and `path`. It SHALL only offer repos that have a non-empty
path (the daemon binds strictly by path, so a path-less row would silently fail in
the control queue). It SHALL HTML-escape the repo label and path it renders so a
name/path containing `<`, `>`, `&` or `"` cannot break the dialog markup.

#### Scenario: One add_repo row per selected repo

- **GIVEN** the add-repo dialog reads the repo list from `_repos!A2:C`
- **WHEN** the human selects one or more repos and confirms
- **THEN** exactly one `_control` row is appended per selected repo
- **AND** each row has `action` = `add_repo`
- **AND** each row's `args` JSON contains `repo` and `path`

#### Scenario: Path-less repos are not offered

- **GIVEN** a `_repos` row with a name but an empty path column
- **WHEN** the add-repo dialog is built
- **THEN** that row is not rendered as a selectable option

#### Scenario: Rendered labels are HTML-escaped

- **GIVEN** a `_repos` row whose name or path contains HTML metacharacters
- **WHEN** the add-repo dialog is built
- **THEN** those characters are escaped in the rendered markup

### Requirement: Create-repo dialog gated by an irreversibility confirm

The create-repo dialog SHALL collect `name`, `template` and `vision`, and SHALL
append ONE `_control` row with `action="create_repo"` and `args` containing `name`,
`template`, `vision` ONLY after an explicit confirm dialog whose text includes
`Создать beelink-<name>?`. Creating a repo is irreversible, so the confirm is the
gate: the append MUST NOT happen before the confirm.

#### Scenario: Confirm precedes the create_repo append

- **GIVEN** the create-repo dialog has `name`, `template`, `vision`
- **WHEN** the human confirms a dialog whose text includes `Создать beelink-<name>?`
- **THEN** exactly one `_control` row is appended with `action` = `create_repo`
- **AND** its `args` JSON contains `name`, `template`, `vision`
- **AND** if the human cancels the confirm, no `_control` row is appended

### Requirement: Run-skill dialog

The run-skill dialog SHALL read the catalog from `_skills!A2:B`, let the human pick one
skill and (optionally) type extra context, and append ONE `_control` row with
`action="run_skill"` and an `args` JSON object containing `skill`, `tab` (the ACTIVE
sheet's title), and `detail`. It SHALL write only to the `_control` tab — never a repo
tab's human cells (A/D/H) or its B1 binding — through the same append helper (A-E only,
`status=pending`, never F). Its client→server callback SHALL be a public function
(no trailing underscore, so `google.script.run` can invoke it).

#### Scenario: run_skill row carries the skill, active tab and detail

- **GIVEN** the run-skill dialog reads the catalog from `_skills!A2:B` and a repo tab is
  the active sheet
- **WHEN** the human picks a skill, optionally types extra context, and confirms
- **THEN** exactly one `_control` row is appended with `action` = `run_skill`
- **AND** its `args` JSON contains `skill`, `tab` (the active sheet title) and `detail`
- **AND** it writes only to the `_control` tab

