# result-log Specification

## Purpose
The format of the task result summary and error written back to the sheet — Russian caveman, без воды.
## Requirements
### Requirement: Agent result summary and error are Russian caveman without fluff

The agent's structured `summary` and `error` fields SHALL be in Russian, in
"caveman" register (short broken phrases, present tense, primitive grammar), and
без воды (terse, no filler), because the daemon writes them verbatim into the
task row's Log cell (column F). The
`RESULT_SCHEMA` field descriptions for `summary` and `error` SHALL state this
requirement, and the agent `SYSTEM_PROMPT` SHALL carry a matching rule, so a
head-less `claude -p` agent (which cannot see this spec) is instructed to produce
those two fields in that register. Machine-relevant tokens embedded in the text
(spec ids, branch names, file paths) SHALL be kept verbatim and not translated.
Only the `summary` and `error` fields are constrained — the rest of the agent's
behaviour (code, commit messages, reasoning) is unchanged.

#### Scenario: Result schema demands Russian caveman summary and error

- **GIVEN** the `RESULT_SCHEMA` used to drive the agent's structured output
- **WHEN** its `summary` and `error` field descriptions are read
- **THEN** each description requires Russian, caveman register, and no fluff

#### Scenario: System prompt instructs the register

- **GIVEN** the agent `SYSTEM_PROMPT`
- **WHEN** it is read
- **THEN** it contains a rule that the `summary` and `error` fields are written
  in Russian caveman style без воды (and that technical tokens stay verbatim)

### Requirement: Daemon-written outcome notes are Russian caveman without fluff

Every outcome note the daemon composes SHALL be Russian caveman без воды before
the daemon writes it to a task row's Log cell (column F) or a `_control` row's
Result cell (column F). This covers: the
immediate run notes written while/at the end of processing a task (the
"agent running" placeholder, the rate-limited requeue note, the
interrupted-by-restart note, and the agent-crashed note); the terminal success
decorations (the spec-ready review hint and the shipped push/deploy marker); the
`blocked` and `failed` fallback notes; and the `_run_skill` outcome note.
Machine-relevant tokens SHALL be preserved verbatim inside these notes — the
skill name, the spec id, the boolean push/deploy ship flags, and any underlying
exception text — so the line stays actionable.

#### Scenario: Spec-ready note is Russian and keeps the approval token

- **WHEN** a gated spec phase parks a row in `spec_ready`
- **THEN** the Log cell note is Russian caveman
- **AND** it still tells the human to set the status to `approved` to ship

#### Scenario: Failed/blocked fallback notes are Russian

- **GIVEN** an agent result with `outcome="failed"` and no summary/error text
- **WHEN** the daemon writes the terminal note
- **THEN** the Log cell holds a Russian caveman fallback (not the word `failed`)

#### Scenario: Skill outcome note is Russian but keeps machine tokens

- **GIVEN** a completed `run_skill` whose agent returned a spec id
- **WHEN** the daemon writes the `_control` Result cell
- **THEN** the note is Russian caveman без воды
- **AND** it still contains the skill name and the spec id verbatim

#### Scenario: Immediate run notes are Russian

- **WHEN** a task is rate-limited, interrupted by a daemon restart, or its agent
  crashes
- **THEN** the Log cell note written for that case is Russian caveman without
  fluff (preserving any underlying exception text)

