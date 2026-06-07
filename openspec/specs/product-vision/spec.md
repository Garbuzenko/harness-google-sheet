# product-vision Specification

## Purpose
The repo task tab's row layout (daemon heartbeat at E1, header on row 2) — there is no Product Vision row.
## Requirements
### Requirement: Sheet layout — heartbeat at E1, no Product Vision row

The repo task tab SHALL have NO Product Vision row. Row 1 is the config row
(`A1`=`REPO_PATH` label, `B1`=binding, `C1`=`BRANCH` label, `D1`=branch) with the
daemon heartbeat in `E1` (row 1, column 5). `HEADER_ROW` SHALL be 2 and
`FIRST_TASK_ROW` SHALL be 3. The daemon SHALL NOT stamp an `A2` `VISION` label,
merge `B2:F2`, or read/write any Product Vision cell. Both GoogleBackend and
MockBackend SHALL write the heartbeat to `E1`.

#### Scenario: Heartbeat writes E1

- **WHEN** the daemon writes a heartbeat on a tab
- **THEN** cell `E1` holds the heartbeat text

#### Scenario: Layout anchors

- **WHEN** the layout constants are read
- **THEN** the heartbeat target is row 1 / column 5, `HEADER_ROW` is 2 and
  `FIRST_TASK_ROW` is 3
- **AND** there is no `VISION_ROW` or `COL_VISION` constant

#### Scenario: Old Product Vision row is migrated away

- **GIVEN** a tab bootstrapped under the old layout — `VISION` in `A2` and the task
  header on row 3
- **WHEN** the schema/migration runs
- **THEN** physical row 2 is deleted so the task header realigns to row 2 and tasks
  to row 3
- **AND** the migration is idempotent: once `A2` no longer reads `VISION` it makes
  no further change

