# live-progress Specification

## Purpose
How the daemon turns an agent's tool-call stream into a live, monotonic progress indicator in the sheet.
## Requirements
### Requirement: Agent runs as a parsed event stream

`agent.run` SHALL invoke `claude -p` with `--output-format stream-json
--verbose` and consume its stdout incrementally, line by line, rather than
buffering a single final JSON blob. Each line that parses as a JSON object is one
event; non-JSON lines SHALL be ignored. The same hard timeout SHALL still bound
the run — on timeout the agent process (its process group) SHALL be killed and an
`outcome="failed"` result returned. The final structured result SHALL be taken
from the stream's `result` event (`structured_output`, falling back to parsing
its `result` text), preserving the existing result schema and rate-limit
detection.

#### Scenario: Streaming run parses the final result

- **GIVEN** a fake `claude` that emits a stream-json transcript ending in a
  `result` event carrying a valid `structured_output`
- **WHEN** `agent.run` executes it
- **THEN** the returned `AgentResult` reflects that structured output
  (`outcome`, `spec_id`, `summary`, ship flags)

#### Scenario: Timeout still bounds a streaming run

- **GIVEN** a fake `claude` that streams forever and never emits a `result`
- **WHEN** `agent.run` executes it with a short timeout
- **THEN** the call returns within roughly the timeout
- **AND** the result is `outcome="failed"` with a timeout error

#### Scenario: Non-JSON noise on the stream is tolerated

- **GIVEN** a transcript that interleaves plain-text/log lines with JSON events
- **WHEN** the stream is consumed
- **THEN** the non-JSON lines are skipped and the run still resolves its result

### Requirement: Deterministic progress tracking

A pure `ProgressTracker` SHALL derive, from the agent's observed `tool_use`
events, a current pipeline **stage** and an approximate **percent**. Stages are
ordered `spec → implement → tests → commit → push → deploy`. Classification SHALL
be by tool/command pattern: OpenSpec commands or edits under `openspec/changes/`
→ `spec`; test-runner invocations (e.g. `pytest`, `npm test`, `go test`) →
`tests`; `git commit` → `commit`; `git push` → `push`; the repo deploy script or
`docker compose`/`docker-compose` → `deploy`; any other file edit/build →
`implement`. Progress SHALL be **monotonic**: the stage index and the reported
percent never decrease. The tracker SHALL NOT perform any I/O.

#### Scenario: Tool calls advance the stage

- **GIVEN** a fresh tracker
- **WHEN** it observes an OpenSpec command, then a source-file edit, then a
  `pytest` run, then `git commit`, then `git push`
- **THEN** the reported stage advances `spec → implement → tests → commit → push`
  in order and never goes backwards

#### Scenario: Out-of-order signals never regress progress

- **GIVEN** a tracker that has already reached the `tests` stage
- **WHEN** it later observes another OpenSpec/spec edit
- **THEN** the reported stage stays at `tests` (or later) and the percent does
  not drop

#### Scenario: Percent ladder scales to the terminal stage

- **WHEN** a tracker's terminal stage is `spec` (spec-only autonomy/phase)
  reaching the `spec` stage reports ~100%
- **AND** for a `ship`/implement run reaching `spec` reports a small percent and
  only `deploy` approaches 100%
- **AND** in every mode the percent stays below 100 until the run completes

### Requirement: Throttled live progress write-back

`agent.run` SHALL accept an optional `on_progress` callback and invoke it with
the tracker's snapshot as the stage/percent change. `_process_task` SHALL supply
a callback that writes a compact progress line to the row's Log (column F) and
refreshes Updated (column E), keeping Status at `working`. The write SHALL be
throttled: a sheet write is emitted only when the stage changes, the percent
rises by at least a fixed delta, or a minimum interval has elapsed — so live
progress can never exhaust the Sheets write quota. The daemon SHALL NOT write any
human-owned cell (A/D/H or B1/B2) for progress, and a failed progress write SHALL
never propagate out of the task.

#### Scenario: Progress line shows stage, percent and autonomy mode

- **WHEN** a task is mid-run at the implement stage under `ship` autonomy
- **THEN** the Log cell holds a compact line conveying the stage, an approximate
  percent, and the autonomy mode (e.g. `⏳ implement ~58% (ship)`)
- **AND** the Status cell still reads `working`

#### Scenario: Redundant progress updates are throttled

- **GIVEN** consecutive snapshots with the same stage and a percent change below
  the delta, within the minimum interval
- **WHEN** they are delivered to the write-back callback
- **THEN** at most the first triggers a sheet write; the others are suppressed

#### Scenario: A progress-write failure cannot kill the task

- **GIVEN** a backend whose `write_cell` raises during a progress update
- **WHEN** the agent reports progress
- **THEN** the exception is swallowed and the task continues to its normal
  terminal status

