# repo-chat Specification

## Purpose
The per-repo read-only chat: a paired chat tab where a human asks questions and the daemon answers from the code.
## Requirements
### Requirement: The compose box advertises itself with a visible placeholder

The daemon SHALL keep the chat compose box (`J1`) self-explanatory so a human can see
WHERE and HOW to talk to the agent without relying on a hover-note. When `J1` is empty,
the daemon SHALL seed a human-readable placeholder string into it; the seed SHALL be
idempotent (skipped when `J1` already holds the placeholder or a real pending question)
so it also migrates existing repo tabs without clobbering a typed question. The
placeholder SHALL be treated as "no question": the daemon SHALL NOT dispatch a chat
turn for it. When a real question is consumed, the daemon SHALL re-seed the placeholder
into `J1` instead of leaving it blank, so the cue is always present.

#### Scenario: An empty compose box is seeded with the placeholder

- **GIVEN** a repo tab whose compose box `J1` is empty
- **WHEN** the daemon bootstraps the tab's schema
- **THEN** `J1` is seeded with the placeholder string
- **AND** a tab whose `J1` already holds the placeholder or a real question is left
  unchanged

#### Scenario: The placeholder is never dispatched as a question

- **GIVEN** a compose box `J1` that holds the placeholder string
- **WHEN** the daemon reads the tab
- **THEN** no chat turn is dispatched (the placeholder counts as an empty box)

#### Scenario: Consuming a question resets the box to the placeholder

- **WHEN** the daemon consumes a real question from `J1`
- **THEN** after the question is echoed into the transcript, `J1` is reset to the
  placeholder string (not left blank)
- **AND** the placeholder is not re-dispatched on the next poll

### Requirement: Chat question/answer cells accept free text

The daemon SHALL pin the chat cells to a text number-format so typed questions and
written answers are never coerced to a number. The compose box (`J1`), the pinned
answer (`K1`), and the transcript region (`J4..`/`K4..`) SHALL be formatted as TEXT
when the chat region is prettified.

#### Scenario: The compose box is formatted as text

- **WHEN** the daemon prettifies the chat region
- **THEN** the compose box, the pinned answer cell and the transcript columns are set
  to a TEXT number-format
- **AND** a digits-only question typed into the box is stored as text, not a number

### Requirement: The chat agent is strictly read-only

The chat agent SHALL run as a short-lived `claude -p` process with the repo as its
working directory, restricted at the tool level to read/search only
(`--allowedTools Read,Grep,Glob`, mutators denied via `--disallowedTools`, and WITHOUT
`--dangerously-skip-permissions`). It SHALL NOT edit, write, run shell commands, commit,
push or deploy. It SHALL return its reply as a structured `{answer}` object. The
conversation transcript on the sheet SHALL be the only memory: the full history is
replayed into each turn's prompt because the process is stateless.

#### Scenario: The chat agent cannot mutate the repo

- **WHEN** a chat turn runs
- **THEN** the agent has only read/search tools available
- **AND** no file edit, commit, push or deploy can occur from the chat path

#### Scenario: A multi-turn conversation keeps context

- **GIVEN** a transcript with prior question/answer turns
- **WHEN** a new question is asked
- **THEN** the prior turns are replayed into the agent's prompt

### Requirement: Chat is responsive and never crashes the supervisor

Chat SHALL be dispatched during the poll cycle BEFORE the OpenSpec gate, so a repo
without `openspec/` can still be discussed. Chat turns SHALL run in a dedicated pool,
separate from the task-agent pool, so a question is not blocked behind a long
implement-and-ship task. The chat path SHALL be fully exception-wrapped: a failed
agent, rate-limit or restart produces a written marker in the answer cell, never an
exception escaping the supervisor loop.

#### Scenario: Chat works on a repo without openspec

- **GIVEN** a bound repo that has no `openspec/` directory
- **WHEN** the human asks a question in the compose box
- **THEN** the chat turn is dispatched and answered (the OpenSpec-only gate applies to
  task implementation, not to read-only chat)

#### Scenario: A failing chat turn does not kill the daemon

- **WHEN** the chat agent errors, is rate-limited, or is interrupted by a restart
- **THEN** a marker is written to the transcript answer cell and pin
- **AND** the supervisor loop continues

### Requirement: The chat tab hides its config (binding) row

The daemon SHALL hide the chat tab's config row (row 1 — the `Репозиторий`/`REPO_PATH`
label in `A1` and the repo binding in `B1`) when it prettifies the chat tab, so the
visible surface is just the compose box (`A2`), the pinned latest answer (`B2`), the
headers (`A3`/`B3`) and the transcript. The hidden row is daemon plumbing the human never
edits. Hiding SHALL be display-only: the binding SHALL still be read by position from
`B1`, and pairing a repo tab with its chat tab by that binding SHALL be unaffected. The
frozen-row count SHALL be unchanged so the compose box and the headers stay frozen and on
screen.

#### Scenario: Prettifying a chat tab hides the config row

- **WHEN** the daemon prettifies a chat tab
- **THEN** row 1 (the `Репозиторий` label and the `B1` binding) is hidden from view
- **AND** the compose box, the pinned answer, the headers and the transcript remain
  visible
- **AND** the compose box and headers stay frozen on screen

#### Scenario: Hiding the row keeps the binding readable

- **GIVEN** a chat tab whose config row is hidden
- **WHEN** the daemon reads the chat tab to resolve its repo
- **THEN** the binding is still read from `B1` by position
- **AND** the chat tab is still paired to its repo tab by that binding

### Requirement: Per-repo chat lives on a dedicated paired chat tab

Each repo SHALL have a companion chat tab, separate from its task tab, that hosts the
read-only chat. The chat tab's title SHALL start with `META_PREFIX` (e.g.
`_chat <repo>`) so the repo-dispatch loop skips it: it is never bootstrapped with the
task schema and no tasks are dispatched from it. The chat tab SHALL use a compact
dedicated layout: `A1` is the `REPO_PATH` label and `B1` the repo binding; `A2` is the
human compose box and `B2` pins the latest answer; `A3`/`B3` are the headers; and
`A4..`/`B4..` are the transcript (oldest first, newest appended at the bottom). The top
rows SHALL be frozen so the compose box and pinned answer stay on screen while the
transcript scrolls. The daemon SHALL own the entire transcript and the `B2` pin, and
SHALL own the compose box `A2` only to the extent of consuming it (reading, then
re-seeding the visible placeholder — never leaving it blank). A repo TASK tab SHALL no
longer host chat: its schema bootstrap SHALL NOT stamp or maintain the old J/K scaffold
and the daemon SHALL NOT read or write J/K on a repo tab.

#### Scenario: Compose box and transcript are parsed from the chat tab

- **GIVEN** a chat tab with the binding in `B1`, text in the `A2` compose box and prior
  turns in `A4:B..`
- **WHEN** the daemon reads the chat tab
- **THEN** `B1` is exposed as the repo binding
- **AND** `A2` is exposed as the pending question
- **AND** the `A4:B..` rows are exposed as the ordered transcript

#### Scenario: A question is consumed atomically

- **WHEN** the daemon picks up a non-empty compose box `A2`
- **THEN** in one batch write it echoes the question into the next transcript row,
  writes a thinking marker into that row's answer cell and the `B2` pin, and resets `A2`
  to the visible placeholder
- **AND** the reset compose box prevents the same question being dispatched twice

#### Scenario: The placeholder is the empty state

- **GIVEN** a chat tab whose compose box `A2` is empty
- **WHEN** the daemon ensures the chat-tab schema
- **THEN** `A2` is seeded with the visible `CHAT_INPUT_PLACEHOLDER`
- **AND** the placeholder is never dispatched as a question
- **AND** a tab whose `A2` already holds the placeholder or a real question is left
  unchanged

#### Scenario: A repo task tab carries no chat

- **WHEN** the daemon bootstraps a repo task tab's schema
- **THEN** no chat compose box, pin, headers or transcript are stamped on it
- **AND** the daemon writes no chat cells on the repo task tab

### Requirement: A repo tab and its chat tab are paired by binding

The daemon SHALL associate a repo tab with its chat tab by matching the repo binding in
`B1`, NEVER by parsing the tab title. The chat tab SHALL carry its own `B1` binding,
identical to the repo tab's binding. Resolving the chat tab's repo, and detecting
whether a pair already exists, SHALL be driven solely by that binding, so renaming a tab
never orphans its chat and a chat tab is never duplicated for an already-paired repo.

#### Scenario: The chat tab resolves its repo by binding

- **GIVEN** a chat tab whose `B1` holds a repo binding (path / git-url / bare name)
- **WHEN** the daemon processes the chat tab
- **THEN** it resolves the repo from `B1` and runs the read-only chat turn against it

#### Scenario: Pairing ignores the title

- **GIVEN** a chat tab bound to a repo whose title does not match the repo tab's title
- **WHEN** the daemon looks for the repo's chat tab
- **THEN** it finds it by the shared `B1` binding regardless of the titles

### Requirement: Provisioning and migration create the chat tab

When a repo tab is provisioned (`add_repo` / `create_repo`) the daemon SHALL also create
its paired chat tab, idempotently: no second chat tab is created when one already exists
for that binding. For repo tabs that predate this behaviour, the daemon SHALL create the
missing paired chat tab on a normal poll cycle. Creating the chat tab is the daemon
acting on the repo's binding and SHALL never write a human-owned cell (A/D/H or the `B1`
binding) of any existing repo tab.

#### Scenario: add_repo creates the pair

- **GIVEN** a pending `add_repo` for a new repo path
- **WHEN** the handler runs
- **THEN** a repo task tab is created and bound to the path
- **AND** a paired chat tab (title starting with `META_PREFIX`) is created whose `B1`
  equals the same path

#### Scenario: Existing repo is migrated on poll

- **GIVEN** a bound repo task tab that has no paired chat tab
- **WHEN** the daemon next collects that tab
- **THEN** a paired chat tab bound to the same binding is created
- **AND** running the cycle again creates no second chat tab for that binding

