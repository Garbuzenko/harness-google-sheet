# spec-id-recording Specification

## Purpose
The daemon records each task's OpenSpec change id in the sheet, derived from the change folder on disk.
## Requirements
### Requirement: The OpenSpec change id is derived from the filesystem

`agent.run` SHALL determine the dispatched task's OpenSpec change id from the
repository filesystem, not solely from the agent's self-reported `spec_id` field
(which is optional in the result schema and frequently omitted). Before launching
the agent it SHALL snapshot the set of change ids present under
`openspec/changes/` and `openspec/changes/archive/`; after the run it SHALL
compute the ids that appeared during the run and resolve the result `spec_id` as
follows:

- In the `implement` phase (a human-approved change is being implemented), the
  handed-in change id SHALL be kept verbatim — the agent must not create a new
  change.
- Otherwise, if exactly one change id appeared during the run, that id SHALL be
  used (authoritative).
- Otherwise the agent's self-reported id SHALL be used when it names a change
  folder that exists on disk.
- Otherwise, if several change ids appeared, the lexically-first new id SHALL be
  used (deterministic).
- Otherwise the agent's self-reported id (possibly empty) SHALL be used as a
  fallback.

The resolved id SHALL also be set on the timeout and no-parseable-result return
paths so a run that authored a change but did not finish still surfaces its id.
The orchestrator persists a non-empty `spec_id` into the sheet's Spec column (B),
so this makes column B reflect the real change folder even when the agent omits
the field.

#### Scenario: Agent creates a change but omits spec_id

- **GIVEN** an `openspec/changes/` directory and a fake agent that creates a new
  change folder `add-foo` during the run but returns a structured result with no
  `spec_id`
- **WHEN** `agent.run` executes it
- **THEN** the returned `AgentResult.spec_id` is `add-foo`

#### Scenario: Disk and report agree

- **GIVEN** a fake agent that creates `add-foo` and also reports
  `spec_id="add-foo"`
- **WHEN** `agent.run` executes it
- **THEN** the returned `AgentResult.spec_id` is `add-foo`

#### Scenario: Implement phase keeps the approved id

- **GIVEN** `agent.run` is invoked with `phase="implement"` and `spec_id="add-foo"`
  for an already-approved change
- **WHEN** the agent runs (creating no new change folder)
- **THEN** the returned `AgentResult.spec_id` is `add-foo`

#### Scenario: Nothing created and nothing reported

- **GIVEN** a fake agent that creates no change folder and reports no `spec_id`
- **WHEN** `agent.run` executes it
- **THEN** the returned `AgentResult.spec_id` is empty

