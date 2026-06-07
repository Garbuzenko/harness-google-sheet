# Change: raise-agent-timeout

## Why

Real implement-and-ship tasks (spec → implement → tests → commit → push → deploy)
routinely run longer than the current default hard timeout of **1800s** (30 min),
so a task that is still making progress gets killed and the row is failed with
`agent timed out after 1800s`. That recurring false failure is the pain point.
Doubling the default to **3600s** (60 min) gives a full task enough headroom while
still bounding a genuinely stuck run.

## What Changes

- Raise the default dispatch hard timeout `AGENT_TIMEOUT` from `1800` to `3600`
  seconds in `config.py`. The value stays env-overridable; only the default moves.
- The chat timeout (`CHAT_TIMEOUT`) is unchanged — chat is short Q&A.

## Non-goals

- No change to the timeout *mechanism* (process-group hard-kill, timeout-salvage
  of an already-emitted result, the reclaim grace derived as
  `max(agent_timeout * 2, 600)`). Only the default number changes.
- No change to `CHAT_TIMEOUT` or `_CREATE_REPO_TIMEOUT`.
- No change to any invariant.
