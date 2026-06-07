# sheet-control-queue Specification

## Purpose
The _control tab — an Apps-Script→daemon intent queue — and how the daemon consumes its rows.
## Requirements
### Requirement: Control intent queue tab

The daemon SHALL maintain a `_control` meta-tab acting as an intent queue with the
frozen header `id|ts|action|args|status|result`. Apps Script owns columns A-D
(id, ts, action, args); the daemon owns ONLY columns E (status) and F (result).
The tab name starts with the meta prefix `_` so the repo-dispatch loops never treat
it as a repo tab. Both the Google and Mock backends SHALL expose behaviourally
equivalent `ensure_control_schema()` and `read_control()`.

#### Scenario: Bootstrap is idempotent

- **WHEN** `ensure_control_schema()` is invoked twice against a backend with no
  prior `_control` tab
- **THEN** the tab exists with the header `id|ts|action|args|status|result` on
  row 1 exactly once
- **AND** no data rows are added by the second invocation

#### Scenario: Daemon never writes columns A-D

- **GIVEN** a `_control` row whose id/ts/action/args (A-D) are set by Apps Script
- **WHEN** the daemon dispatches that row
- **THEN** columns A-D are left byte-for-byte unchanged
- **AND** only columns E (status) and F (result) are written

#### Scenario: read_control returns rows oldest-first

- **GIVEN** several `_control` rows with timestamps out of sheet-row order
- **WHEN** `read_control()` is called
- **THEN** the returned rows are sorted oldest-first by `(ts, row)` so the oldest
  pending intent is dispatched first

### Requirement: Control intent dispatcher

Each supervisor cycle the daemon SHALL process `_control` rows oldest-first,
transitioning each pending row `pending → working → done|error`. Dispatch SHALL be
idempotent by `id` (a row already in `done` or `error` is skipped and never
re-invoked), SHALL reclaim rows stuck in `working` past the grace window back to
`pending` using the same age-based logic as the task stale-reclaim, and SHALL never
raise out of the supervisor loop. Actions are resolved through an extensible
action→handler registry; an unknown action marks the row `error`.

#### Scenario: Pending rows processed oldest-first

- **GIVEN** multiple pending `_control` rows
- **WHEN** a dispatch cycle runs
- **THEN** rows are processed oldest-first and only rows whose status is `pending`
  are dispatched

#### Scenario: Done or error rows are skipped (idempotent by id)

- **GIVEN** a `_control` row already in `done` (or `error`)
- **WHEN** a dispatch cycle runs
- **THEN** its handler is NOT re-invoked
- **AND** its `result` (column F) is not overwritten

#### Scenario: Unknown action marks the row error

- **GIVEN** a pending `_control` row whose action has no registered handler
- **WHEN** the dispatcher processes it
- **THEN** the row is marked `error` with a result containing `unknown action`
- **AND** the daemon does not crash

#### Scenario: Raising handler or malformed args cannot kill the daemon

- **GIVEN** a pending row whose handler raises, and a pending row with malformed
  JSON `args`
- **WHEN** the dispatch cycle runs
- **THEN** no exception propagates out of the control cycle or the supervisor loop
- **AND** each offending row is marked `error` with the message in column F
- **AND** subsequent pending rows are still processed

#### Scenario: Stale working rows reclaimed, fresh ones untouched

- **GIVEN** a `_control` row stuck in `working` older than the grace window and a
  freshly-`working` row within the window
- **WHEN** the dispatch cycle runs
- **THEN** the stale row is reclaimed back to `pending`
- **AND** the fresh `working` row is left untouched

### Requirement: Asynchronous run_skill action

The control dispatcher SHALL recognise the `run_skill` action as ASYNCHRONOUS: because a
skill run is real, long delivery work (up to the agent timeout), the daemon SHALL NOT run
it inline in the synchronous control dispatcher (which would block the poll loop). Instead
the daemon SHALL submit the run to the background agent pool, subject to the SAME per-repo
in-flight serialization as task agents (never two agents in one working dir), and SHALL
report progress back into the control row: `working` while it runs, then `done` with a
summary or `error` with the failure. A rate-limit or daemon-restart interruption SHALL
requeue the row to `pending` rather than burning it to `error`. A repo already in-flight
SHALL leave the row `pending` for a later cycle. The dispatch SHALL never raise out of the
supervisor loop, and SHALL leave columns A-D byte-for-byte unchanged.

#### Scenario: run_skill is dispatched asynchronously, not inline

- **GIVEN** a pending `run_skill` control row
- **WHEN** a dispatch cycle runs
- **THEN** the run is submitted to the background agent pool and the poll loop is not
  blocked for the duration of the agent
- **AND** the control row is marked `working` while it runs and `done`/`error` at the end
- **AND** columns A-D are left byte-for-byte unchanged

#### Scenario: An in-flight repo defers the run_skill row

- **GIVEN** a `run_skill` control row whose target repo already has an agent in flight
- **WHEN** a dispatch cycle runs
- **THEN** the row is left `pending` (not dispatched) and retried on a later cycle

