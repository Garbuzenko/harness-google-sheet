# task-grid-layout Specification

## Purpose
The columns and headers of the repo task grid — A–F, Russian headers, hidden Tries, colour-coded status.
## Requirements
### Requirement: The repo task grid has no Detail column

The repo task tab SHALL use a 7-column task grid `A–G` with no human "Detail"
column. The columns SHALL be, in order: Task (A, human), Spec (B, daemon),
Status (C, daemon/human-actionable), Updated (D, daemon), Log (E, daemon),
Tries (F, daemon), Priority (G, human). `HEADERS` SHALL contain exactly these
seven labels, and `parse_grid` SHALL determine whether a row is non-empty from
the Task/Spec/Status cells only (not a Detail cell).

The per-task extra-context channel used by the `run_skill` path is unaffected:
`agent.run` SHALL keep its `detail` parameter, the skill dispatch SHALL keep
passing the human's dialog `detail`, and the ordinary task dispatch SHALL pass no
detail.

#### Scenario: parse_grid reads the 7-column layout

- **GIVEN** a grid whose header row is `Task, Spec, Status, Updated, Log, Tries,
  Priority` and a task row `t1, "", queued, "", "", 2, 9`
- **WHEN** `parse_grid` parses it
- **THEN** the parsed row's task is `t1`, status is `queued`, tries is `2` and
  priority is `9`
- **AND** the parsed row has no Detail attribute

#### Scenario: HEADERS carries no Detail

- **WHEN** the fixed `HEADERS` constant is read
- **THEN** it equals `["Task", "Spec", "Status", "Updated", "Log", "Tries",
  "Priority"]`

### Requirement: Existing tabs are migrated by dropping physical column D

The daemon SHALL migrate a repo tab that still carries the old 8-column layout
(the cell `D3` reads `Detail`) by deleting its physical column D, so the remaining
daemon-owned data (Updated/Log/Tries/Priority) realigns to the new 7-column
layout, and it SHALL realign the in-memory grid it is about to parse so the same
poll cycle reads the new layout. The migration SHALL be idempotent: once `D3` no
longer reads `Detail` it is a no-op. Both `GoogleBackend` and `MockBackend` SHALL
expose a behaviourally-equivalent `delete_column(title, col)` seam used by the
migration.

#### Scenario: Old tab has its Detail column removed

- **GIVEN** a tab bootstrapped under the old 8-column layout (header
  `Task, Spec, Status, Detail, Updated, Log, Tries, Priority`) with a task row
  whose Priority sits in column H
- **WHEN** the daemon bootstraps the tab
- **THEN** physical column D is deleted, the header row becomes the 7-column layout
  and the task row's Priority sits in column G
- **AND** a second bootstrap deletes nothing (the migration is a no-op)

### Requirement: The repo task grid has no Priority column and hides Tries

The repo task tab SHALL use a 6-column task grid `A–F` with no human "Priority"
column. The columns SHALL be, in order: Task (A, human), Spec (B, daemon),
Status (C, daemon/human-actionable), Updated (D, daemon), Log (E, daemon),
Tries (F, daemon). `HEADERS` SHALL contain exactly these six labels and SHALL NOT
contain `"Priority"`. No `COL_PRIORITY` or `DEFAULT_PRIORITY` constant SHALL
exist, and `parse_grid` SHALL determine whether a row is non-empty from the
Task/Spec/Status cells only.

Column F (Tries) is daemon-owned durable state, not human-facing: the daemon
SHALL keep writing the attempt counter to column F, and the formatting pass SHALL
hide column F from the human (`hiddenByUser`) so the visible grid spans A–E. The
dead-lettering attempt cap, the retry/approved attempt reset, and the rate-limit
backoff SHALL continue to read and write Tries unchanged.

#### Scenario: parse_grid reads the 6-column layout

- **GIVEN** a grid whose header row is `Task, Spec, Status, Updated, Log, Tries`
  and a task row `t1, "", queued, "", "", 2`
- **WHEN** `parse_grid` parses it
- **THEN** the parsed row's task is `t1`, status is `queued` and tries is `2`
- **AND** the parsed row has no Priority attribute

#### Scenario: HEADERS carries no Priority

- **WHEN** the fixed `HEADERS` constant is read
- **THEN** it equals `["Task", "Spec", "Status", "Updated", "Log", "Tries"]`

### Requirement: Tasks dispatch in stable sheet order

With no Priority column there SHALL be no priority-based ordering. The
orchestrator SHALL order ready work stably by `(tab title, row)` so tasks run in
the order they appear in the sheet.

#### Scenario: ready work is ordered by row

- **GIVEN** two ready task rows on the same tab at rows 5 and 4
- **WHEN** the orchestrator sorts the work items for dispatch
- **THEN** the row-4 item is ordered before the row-5 item

### Requirement: Existing tabs are migrated by dropping physical column G

The daemon SHALL migrate a repo tab that still carries the old 7-column layout
(the cell `G3` reads `Priority`) by deleting its physical column G, so the
remaining daemon-owned data realigns to the new 6-column layout, and it SHALL
realign the in-memory grid it is about to parse so the same poll cycle reads the
new layout. The migration SHALL be idempotent: once `G3` no longer reads
`Priority` it is a no-op. The migration SHALL reuse the existing
`delete_column(title, col)` seam present on both `GoogleBackend` and
`MockBackend`.

#### Scenario: Old tab has its Priority column removed

- **GIVEN** a tab bootstrapped under the old 7-column layout (header
  `Task, Spec, Status, Updated, Log, Tries, Priority`) with a Tries value in
  column F and a Priority value in column G
- **WHEN** the daemon bootstraps the tab
- **THEN** physical column G is deleted, the header row becomes the 6-column
  layout and the task row's Tries still sits in column F
- **AND** a second bootstrap deletes nothing (the migration is a no-op)

### Requirement: The repo task grid uses Russian, human-friendly labels

The repo task tab SHALL present a Russian header and config row. `HEADERS` SHALL
equal `["Задача","Спека","Статус","Обновлено","Итог","Попытки"]` (the same
6-column A–F layout, with Попытки/Tries still hidden). The config row SHALL label
`A1` `Репозиторий` (the repo binding lives in `B1`) and `C1` `Ветка` (the branch
in `D1`). Column ownership and meaning SHALL be unchanged: the human owns `A`
(Задача) and the `B1` binding; the daemon owns `B/C/D/E/F` and the heartbeat.

#### Scenario: HEADERS is the Russian six-column set

- **WHEN** the fixed `HEADERS` constant is read
- **THEN** it equals `["Задача","Спека","Статус","Обновлено","Итог","Попытки"]`

#### Scenario: a freshly bootstrapped tab carries Russian labels

- **GIVEN** an empty repo tab
- **WHEN** the daemon bootstraps its schema
- **THEN** `A1` reads `Репозиторий`, `C1` reads `Ветка` and the header row reads
  the Russian `HEADERS`

### Requirement: Status is colour-coded

The formatting pass SHALL apply conditional-format rules that paint the Status
column (rows from `FIRST_TASK_ROW` down) with a distinct background colour per
status value, covering every status in `ALL_STATUSES`. The rule set SHALL be
applied idempotently: any conditional-format rules already present on the sheet
are cleared first, then the per-status rules are added, so repeated formatting
never accumulates duplicate rules.

#### Scenario: every status has a colour

- **WHEN** the per-status conditional-format rules are built for a sheet
- **THEN** there is exactly one rule per status in `ALL_STATUSES`, each matching
  the status text and setting a background colour

### Requirement: Existing tabs are migrated to the Russian labels in place

The daemon SHALL migrate a repo tab still on the old English labels by rewriting
`A1` (`REPO_PATH` → `Репозиторий`), `C1` (`BRANCH` → `Ветка`) and the header row
(`Task,…` → the Russian `HEADERS`), realigning the in-memory grid it is about to
parse so the same poll cycle reads the new labels. The migration SHALL be
idempotent (a no-op once the labels are already Russian) and SHALL NOT alter the
`B1` binding or any task data. Schema-initialisation detection SHALL treat a tab
carrying either the old or the new labels as initialised, so the migration never
triggers a re-bootstrap. On a backend that supports formatting, a tab the
migration changed SHALL be re-prettified so the status colours and widths land on
already-bootstrapped tabs.

#### Scenario: an old English tab is relabelled

- **GIVEN** a tab whose `A1` reads `REPO_PATH`, `C1` reads `BRANCH` and whose
  header row reads `Task, Spec, Status, Updated, Log, Tries`
- **WHEN** the daemon ensures the schema
- **THEN** `A1` reads `Репозиторий`, `C1` reads `Ветка`, the header row reads the
  Russian `HEADERS`, the `B1` binding is unchanged
- **AND** a second ensure-schema rewrites nothing (the migration is a no-op)

