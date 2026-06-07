# skills-catalog Specification

## Purpose
The _skills catalog tab and the two ways a human runs a catalog skill on a repo from the sheet.
## Requirements
### Requirement: Skills catalog reference tab

The daemon SHALL maintain a `_skills` meta-tab acting as a curated catalog of runnable
skills, with the frozen header `Skill | Description | Prompt`: column A is the skill
name (the picker value), column B a human-readable description, column C the prompt the
daemon feeds the agent when the skill is run. The tab name starts with the meta prefix
`_` so the repo-dispatch loops never treat it as a repo tab. Both the Google and Mock
backends SHALL expose behaviourally equivalent `ensure_skills_tab()` and `read_skills()`.

Unlike the discovery-rebuilt `_repos` tab, `_skills` is human-curated: the daemon SHALL
seed the catalog ONLY when the tab is absent or has no header (first creation) and SHALL
NOT overwrite the tab on later calls, so an operator's prunes and prompt edits are never
clobbered. The seeded catalog SHALL be broad (so the operator can prune) and SHALL
include an `autopilot` skill.

#### Scenario: Seed is idempotent and never clobbers edits

- **WHEN** `ensure_skills_tab()` is invoked twice against a backend with no prior
  `_skills` tab
- **THEN** the tab exists with the header `Skill | Description | Prompt` on row 1 exactly
  once and is seeded with the default catalog
- **AND** the second invocation overwrites neither the header nor any catalog row

#### Scenario: read_skills returns the catalog rows

- **GIVEN** a seeded `_skills` tab
- **WHEN** `read_skills()` is called
- **THEN** it returns one entry per non-empty catalog row, each carrying the skill name,
  description and prompt
- **AND** the catalog includes a skill named `autopilot`

### Requirement: Run a catalog skill against a repo tab

The daemon SHALL be able to run any catalog skill against a bound repo tab, driven by a
`run_skill` control intent carrying `{skill, tab}` (and an optional free-text `detail`).
The skill's PROMPT SHALL be looked up from the `_skills` tab (column C) at dispatch time,
so the sheet remains the single source of truth for what a skill does. The run SHALL go
through the SAME OpenSpec-gated agent path that tasks use, so the OpenSpec-only invariant
holds; the optional `detail` SHALL be supplied to the agent as extra context.

A `run_skill` intent SHALL never cause the daemon to write a repo tab's human-owned cells
(A/D/H) or its B1 binding — the run is materialised as a control-row intent, not a task
row.

#### Scenario: A run_skill intent dispatches the skill's prompt

- **GIVEN** a `_skills` catalog containing a skill and a `run_skill` control row naming
  that skill and a bound repo tab
- **WHEN** the daemon dispatches the row
- **THEN** an agent is run against the bound repo with the skill's prompt as the task
- **AND** the run uses the same OpenSpec-gated path as a normal task
- **AND** no repo tab's A/D/H or B1 is written

#### Scenario: Unknown skill or unresolvable tab marks the row error

- **GIVEN** a `run_skill` control row whose `skill` is not in the catalog, or whose `tab`
  cannot be resolved to a repo
- **WHEN** the daemon dispatches the row
- **THEN** the control row is marked `error` with a clear result
- **AND** the daemon does not crash

### Requirement: Comprehensive seeded skill catalog

The default catalog seeded into `_skills` SHALL be comprehensive: beyond the
`autopilot` skill it SHALL cover the main families of delivery work an agent can run
against a repo, so an operator can pick a useful playbook without authoring one. The
seed SHALL include, at minimum, at least one skill for each of:

- testing (e.g. raising coverage, de-flaking tests),
- code quality (e.g. simplifying / removing dead code),
- robustness & operability (e.g. hardening, retries/timeouts, input validation,
  logging/metrics),
- security,
- performance,
- documentation (e.g. README/docstrings, API docs, onboarding),
- engineering hygiene (e.g. reviewing recent changes, CI, type coverage, lint/format),
- running-product UX (e.g. walking the live surface to fix UX, improving page speed,
  accessibility).

Each seeded skill SHALL carry a non-empty name, a human-facing description and an
agent-facing prompt, and each prompt SHALL be a self-contained task statement runnable
through the same OpenSpec-gated `agent.run` path as a normal task (no prompt may depend
on a Claude Code slash command or skill, which are unavailable under `claude -p`). The
catalog remains human-curated and seed-once, so the breadth is a starting point the
operator prunes — never re-seeded over their edits.

#### Scenario: Seeded catalog spans the delivery families

- **WHEN** `read_skills()` is called against a freshly seeded `_skills` tab
- **THEN** it returns the `autopilot` skill plus additional skills covering testing,
  code quality, security, performance, documentation, engineering hygiene and
  running-product UX
- **AND** every returned skill has a non-empty name, description and prompt

#### Scenario: Every seeded prompt is self-contained

- **GIVEN** the default catalog
- **WHEN** each skill's prompt is inspected
- **THEN** no prompt instructs the agent to invoke a Claude Code slash command or skill
- **AND** each prompt reads as a standalone task the OpenSpec ship pipeline can run

### Requirement: The running daemon self-seeds the skills catalog

The daemon's poll cycle SHALL ensure the `_skills` catalog tab exists on every
cycle, so an operator running ONLY the supervisor service (`sheet_agent run`) — the
documented deploy path — sees the catalog and can use the `▶️ Запустить скилл` menu
without first running a `bootstrap`/`skills` CLI command. The cycle SHALL call
`ensure_skills_tab()`, which is seed-once and quota-friendly: it seeds the default
catalog only on first creation, caches confirmation so subsequent cycles incur no
extra Sheets read, and never clobbers an operator's prunes or prompt edits.

This ensure SHALL be best-effort and exception-wrapped: a `_skills` failure SHALL
NOT block repo-task collection and SHALL NOT raise out of the cycle, preserving the
"the supervisor must never die" invariant. Both the `run` loop and the one-shot
`once` command SHALL get this behaviour, since both go through the same cycle.

#### Scenario: One poll cycle creates the catalog for a daemon-only operator

- **GIVEN** a backend with no prior `_skills` tab
- **WHEN** a single supervisor poll cycle runs
- **THEN** the `_skills` tab exists with the header `Skill | Description | Prompt`
  and is seeded with the default catalog
- **AND** `read_skills()` returns the catalog including a skill named `autopilot`

#### Scenario: Repeated cycles never clobber operator edits

- **GIVEN** a `_skills` tab that an operator has pruned/edited
- **WHEN** further poll cycles run
- **THEN** the catalog rows and header are left unchanged
- **AND** the per-cycle ensure performs no overwrite

#### Scenario: A skills-tab failure never kills the cycle

- **GIVEN** a backend whose `ensure_skills_tab()` raises
- **WHEN** a poll cycle runs
- **THEN** the cycle does not raise out (the supervisor never dies)
- **AND** repo-task collection still proceeds

### Requirement: A skill can be run from a repo task cell with a slash trigger

A human SHALL be able to run a catalog skill by typing `/<скилл>` into a repo task
cell (column `A`), optionally followed by free-text extra context
(`/<скилл> <контекст>`). The daemon SHALL resolve the skill by name against the
`_skills` catalog, run the skill's **prompt** as the task on the tab's bound repo
through the normal OpenSpec-gated path, and forward the trailing text as the
skill's `detail` (extra context). The daemon SHALL NOT write column `A`; status,
spec id and the result log are written to `B/C/D/E/F` exactly as for an ordinary
task. An unknown skill name SHALL fail that row with a message pointing the human
to the `_skills` tab, instead of running the literal text as a task.

#### Scenario: slash trigger runs the skill's prompt

- **GIVEN** a `_skills` catalog containing a skill `idea` with prompt `P`
- **AND** a repo task cell reading `/idea focus on auth`
- **WHEN** the daemon plans the row
- **THEN** the dispatched work runs prompt `P` with detail `focus on auth`
- **AND** the human's task cell `A` is never overwritten by the daemon

#### Scenario: unknown skill fails the row

- **GIVEN** a `_skills` catalog with no skill named `nope`
- **AND** a repo task cell reading `/nope`
- **WHEN** the daemon plans the row
- **THEN** the row is marked failed with a message naming the `_skills` tab
- **AND** no agent is dispatched for that row

### Requirement: The skills catalog shows how to run each skill

The `_skills` catalog SHALL use Russian headers and SHALL expose a visible
`Запуск` column whose value for each skill is the exact trigger string
`/<скилл>`, so the human can see and copy how to run it. The catalog columns
SHALL be, in order: `Скилл` (A, picker/name), `Описание` (B), `Промпт` (C,
agent-facing) and `Запуск` (D). Seeding SHALL remain seed-once (only when the tab
is absent/empty). An already-seeded English catalog SHALL be migrated in place:
the header row is Russianised and the `Запуск` column is filled, without
re-seeding rows or clobbering curated `Описание`/`Промпт` text or prunes. The
migration SHALL be idempotent and detection of an initialised catalog SHALL accept
both the old (`Skill`) and new (`Скилл`) name header.

#### Scenario: catalog exposes the trigger string

- **GIVEN** a freshly seeded `_skills` catalog containing a skill `idea`
- **THEN** that row's `Запуск` cell reads `/idea`
- **AND** the header row reads `Скилл, Описание, Промпт, Запуск`

#### Scenario: an existing English catalog is migrated without clobbering curation

- **GIVEN** a `_skills` catalog with header `Skill, Description, Prompt` and a
  curated row whose `Промпт` was hand-edited
- **WHEN** the daemon ensures the catalog
- **THEN** the header becomes `Скилл, Описание, Промпт, Запуск`, the row's `Запуск`
  is filled with its `/<скилл>` trigger and the curated prompt is unchanged
- **AND** a second ensure rewrites nothing (the migration is a no-op)

