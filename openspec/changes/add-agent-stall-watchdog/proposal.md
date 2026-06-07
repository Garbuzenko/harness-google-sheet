# Change: add-agent-stall-watchdog

## Why

The only guard on a dispatched agent is the coarse wall-clock `AGENT_TIMEOUT`
(3600s). When an agent runs a bash tool call that never returns — e.g. an
unbounded `until … grep` verify loop over ssh, or a child process (a stray
`node server.js`, an ssh remote command) that holds the stdout pipe open so the
`claude` process itself blocks — the stream goes silent and the task sits frozen
at its last progress (observed: a real task wedged at `~98%` on a verify probe and
would have burned the full hour before the wall-clock killed it). One hour of a
scarce concurrency slot is wasted and the human sees a dead `working` row.

Two failure modes need bounding tighter than the wall-clock:

1. **Naive unbounded tool calls** (the common case) — a quick check the agent
   expects to finish in seconds that instead loops forever.
2. **Pipe-wedge** — a tool "times out" at the agent layer but the spawned child
   does not die and keeps the output pipe open, so `claude` never advances. The
   agent-layer tool timeout cannot save this; only an external hard-kill can.

## What Changes

A two-layer harness, both layers env-overridable with sane defaults.

- **Layer 1 — bound each tool call (inside the agent).** When spawning `claude -p`
  (both the implement and chat paths), inject `BASH_DEFAULT_TIMEOUT_MS` (default
  **120000** = 2 min) and `BASH_MAX_TIMEOUT_MS` (default **900000** = 15 min) into
  the child env. The default kills naive unbounded bash fast; the high ceiling
  still leaves room for a legitimate cold `docker compose build` deploy (these
  repos build images on the server). `_start_reader` gains an optional `env`
  argument; both `run()` and `chat()` pass the augmented environment.
- **Layer 2 — stall watchdog (control from above, in the daemon).** The read loop
  already wakes every ~2s even while the child is silent. Track the timestamp of
  the last stream line; if no line arrives for `AGENT_STALL_TIMEOUT` (default
  **1200** = 20 min, for the implement path) the run is treated like a hard
  timeout: hard-kill the process group (SIGKILL on the group takes the wedged
  child too), salvage an already-emitted `result` if present, otherwise fail the
  row with a distinct `error`/`summary` naming the stall so the human sees *why*.
  The chat path gets the same watchdog with a tighter `CHAT_STALL_TIMEOUT`
  (default **90** s — chat is read-only Read/Grep, sub-second tool calls).
- `AGENT_STALL_TIMEOUT` SHALL be ≥ `BASH_MAX_TIMEOUT_MS` so a legitimate
  max-length bash (which is silent on the stream until it returns) cannot trip the
  watchdog; the gap (15 min ceiling vs 20 min stall) is the safety margin.

## Non-goals

- No change to `AGENT_TIMEOUT` (3600s) — it stays as the coarse backstop for a
  "busy but never finishing" agent (steady tool calls, no real progress).
- No change to the timeout-salvage path, the reclaim grace, or any invariant.
- No new local source of truth — the stall outcome is written to the sheet row
  exactly like the existing timeout outcome.
