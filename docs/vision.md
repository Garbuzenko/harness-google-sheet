# Product Vision — `harness-google-sheet` (sheet-agent)

> A Google Sheet becomes the control plane for a fleet of autonomous coding
> agents. You type a task into a row; a never-dying supervisor turns it into an
> OpenSpec change, implements it, tests it, ships it, and writes the status back
> into the cell next to you. No terminal, no IDE, no babysitting a chat window.

---

## 1. The bet

Software delivery is gated less by writing code than by **orchestrating** it:
keeping context, holding a spec, running the loop (spec → build → test → commit →
push → deploy), recovering from crashes, and knowing *what state everything is
in*. Autonomous coding agents are now good enough to do the writing. The missing
piece is a **durable, legible, operable control surface** that a human can drive
without becoming the orchestration runtime themselves.

The bet of this project: **a spreadsheet is that surface.**

A sheet is something almost everyone already knows how to use. It is a grid of
durable state, multi-user, shareable with one click, readable at a glance, and
it survives every process that touches it. Turn each tab into a repository and
each row into a task, and you have a software factory whose entire interface is
*"write what you want in a cell."*

## 2. The problem we refuse to accept

The obvious way to run autonomous agents is **one long-lived session**. It is
also the wrong way:

- A 24/7 agent **rots its own context** — the longer it runs, the worse it
  reasons about what it is doing.
- A crash **loses everything** — there is no durable record of where it was.
- You cannot *see* its state without reading a transcript, and you cannot *share*
  that state with anyone who does not live in your terminal.

The second-obvious way is a **CI bot** (issue → PR). That is durable and legible,
but it is built for engineers, lives on a developer platform, and treats "ship it"
as a human's job. It does not let a non-engineer run the loop end to end.

We want both halves: the **durability and legibility** of an issue tracker, and
the **reach** of a tool anyone can open — plus genuine end-to-end autonomy.

## 3. The shape of the answer

> **A dumb, unkillable supervisor + short, disposable agents, with the sheet as
> the single source of truth.**

- The **supervisor** is trivial and never dies (systemd `Restart=always`, linger,
  every cycle and task exception-wrapped, single-instance lock). It owns no
  intelligence — it polls, dispatches, and writes back.
- Each task runs in a **fresh, isolated `claude -p` process** with a hard timeout.
  Fresh context every time means no rot; isolation means one poisoned task cannot
  take down the fleet; a hung agent is killed and the row is marked `failed`.
- The **sheet is the durable state.** A restart resumes exactly where it left off,
  because "where it left off" was never in memory — it was always in the grid.
  There is no second source of truth that can drift.

This is the whole architecture, and it is deliberately boring. Boring is the
feature: the part that must never fail is too simple to fail, and the part that is
allowed to fail (the agent) is cheap to replace.

One corollary: **intelligence is rented only where it is safe to be wrong.**
Open-ended task work is delegated to the agent. Structural, irreversible
operations — scaffolding and creating a new repo (`create_beelink_repo.sh`),
registering it, pushing it — run through a **deterministic engine with no LLM in
the critical path**. The same script backs both the human CLI and the daemon: one
engine, two entry points, no model guessing where a guess would cost you a repo.

## 4. Discipline is not optional: OpenSpec-first

Autonomy without discipline produces fast garbage. Every task here is first an
**OpenSpec change** — a written proposal and spec, validated `--strict`, *before*
any code is written. The supervisor refuses repos that have no `openspec/`
directory.

This makes the agent's work **reviewable and reversible**: the spec is the
artifact a human reads to decide whether to trust the change. It is also what
makes the `gated` autonomy level meaningful — the agent writes the spec and parks;
a human approves; only then does code get written and shipped. Spec-first is the
seatbelt that makes high autonomy safe enough to leave running.

## 5. Who it is for

1. **The solo operator running a whole portfolio.** This is the proven case: one
   person **developing 10–20 projects in parallel** and maintaining the
   infrastructure under all of them — not by opening 20 terminals, but by adding
   rows to one sheet. The harness lets a single human carry a workload that would
   normally need a team, because the orchestration is no longer theirs to hold.
   Autonomy is dialled per repo (`spec` / `code` / `ship` / `gated`) so
   production repos stay gated while throwaway ones ship themselves, and live
   status across every tab tells you what the whole fleet is doing at a glance.

2. **The non-engineer building from scratch.** This is the reach the spreadsheet
   unlocks, and the most ambitious promise here. A friend or partner **who cannot
   program at all** is handed a **scoped** control plane — a separate "friend"
   file exposing only the projects you choose — and through it they **build a new
   product from nothing**, one typed task at a time. They never see a terminal, a
   repo, or a line of code; they describe what they want, the agents build it, and
   the sheet shows them where it stands. `gated` autonomy keeps the owner in the
   approval loop by default. Programming stops being the barrier to creating
   software.

## 6. What the experience feels like

- **Filing work is typing a sentence.** A task is a cell. A *playbook* is a
  `/skill` in a cell (`harden`, `add-tests`, `pagespeed`, `autopilot`, …) drawn
  from a human-curated catalog.
- **State is ambient.** Status is colour-coded; a working row shows live,
  deterministic progress (`⏳ implement ~58% (ship)`); the final result and the
  full spec land back in the row. You never ask "what is it doing?" — the grid
  already answers.
- **It is conversational where it needs to be.** Each repo has a read-only chat
  tab for asking questions about the code without risking a write.
- **It is unkillable.** Reboots, logouts, crashes, API 429s, hung agents — none of
  them lose work or stop the loop. The worst case is a row marked `failed` that
  you set back to `retry`.

## 7. What this deliberately is **not**

- **Not a chat UI for coding.** The interface is structured state, not a
  conversation transcript. Conversation is the narrow exception (the chat tab),
  not the medium.
- **Not a CI/CD replacement.** It *drives* deploy scripts; it does not reinvent
  pipelines, runners, or environments.
- **Not an IDE or an agent framework.** It is a supervisor and a control surface.
  The intelligence is rented from `claude -p`, kept deliberately at arm's length.
- **Not a second database.** The sheet is authority. Local `state/` is forensics
  and locks only — never a source of truth that can drift from the grid.

## 8. Why these constraints are the product

The invariants are not incidental engineering preferences — they *are* the value
proposition:

| Invariant | The promise it keeps |
|---|---|
| The supervisor must never die | You can leave it running and forget about it |
| The sheet is the durable state | Restart resumes; nothing is lost; anyone can read it |
| OpenSpec-only | Autonomy stays reviewable, reversible, and safe to gate |
| Fresh agent per task | Quality does not decay over time or across tasks |
| Daemon owns its columns; you own yours | The human and the machine never overwrite each other |

Break any one of them and the product stops being the thing people can trust.

## 9. Where it is going

The trajectory is **from one operator to many, from many repos to shared repos**:

- **Friend sheets (in progress).** A scoped, per-partner control plane —
  registry, mint-and-share flow, per-sheet repo allowlist and autonomy, and the
  live multi-sheet poll loop so every partner's writes land on their own file and
  never the master. The owner provisions the empty repo and shares it; the friend
  then **grows the entire product into it from scratch** through tasks. (Repo
  *creation itself* stays an owner-only power for now — the safe boundary while a
  non-engineer drives everything inside it.) This is how "anyone can open a
  spreadsheet" turns into "anyone can ship a product."
- **Richer playbooks.** The `_skills` catalog grows into the shared, curated
  library of "how we deliver" — code quality, hardening, security, performance,
  accessibility, docs, observability — runnable by name from any sheet.
- **Community-ready.** The daemon, the setup, and the docs polished so a stranger
  can stand up their own sheet-driven factory in an afternoon.

The long horizon: a control plane that a small team — engineers and non-engineers
alike — operates entirely from spreadsheets, where shipping software is as
ordinary an act as updating a row.

## 10. How we will know it is working

- **Terminal-free throughput.** Tasks shipped per week where a human never opened
  a terminal — only typed into a cell.
- **Time from cell to deploy.** The latency from "task typed" to "change live,"
  trending down.
- **Uptime of the loop.** Days the supervisor runs unattended without losing a
  task. The target is "you forget it is running."
- **Non-engineer reach.** Partners successfully driving delivery through a friend
  sheet without ever touching a repo, a CLI, or you.

---

*The north star, in one line: **make shipping software feel like editing a
spreadsheet — durable, legible, and operable by anyone you choose to hand a row.***
