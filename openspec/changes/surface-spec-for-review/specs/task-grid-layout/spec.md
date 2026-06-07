# Spec Delta: task-grid-layout

## ADDED Requirements

### Requirement: Gated spec is reviewable from the row

The daemon SHALL make a gated spec reviewable from its row. When a task completes
its spec phase under `gated` autonomy (status `spec_ready`), the daemon SHALL attach
the change's human-readable body — its `proposal.md` and every `specs/**/spec.md`
delta, read from the repo's `openspec/changes/<id>/` — as a cell NOTE on the `Спека`
cell (`COL_SPEC`) of the originating sheet, capped at
`COL_SPEC_NOTE_MAX`. The change id SHALL remain the cell's value; the body SHALL be
the note (read on hover/click, so the row height is unchanged). The `Итог`/Log
message SHALL point the human to that note. Reading the spec files SHALL be
best-effort: if they are absent or unreadable, the row SHALL fall back to the
id-only message with no note and no failure. The note SHALL be written to the
originating sheet's backend, so a friend-sheet spec is reviewable on the friend
sheet, never the master.

#### Scenario: A ready spec attaches its proposal and deltas as a note

- **GIVEN** a `gated` repo tab whose task runs the spec phase and returns `spec_ready`
  with change id `add-x`, and `openspec/changes/add-x/proposal.md` + a spec delta
  exist in the repo
- **WHEN** the daemon records the result
- **THEN** the row status is `spec_ready` and column B holds `add-x`
- **AND** the `Спека` cell has a note containing the proposal and the spec delta
- **AND** the Log message tells the human to read that note before approving

#### Scenario: Missing spec files fall back without crashing

- **GIVEN** a `spec_ready` result whose change folder cannot be read
- **WHEN** the daemon records the result
- **THEN** no note is attached and the id-only "ставь статус approved" message is used
- **AND** the task is not failed or blocked by the missing files
