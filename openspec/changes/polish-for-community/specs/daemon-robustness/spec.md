# Spec Delta: daemon-robustness

## ADDED Requirements

### Requirement: Daemon-owned cross-thread state is read under the lock

Daemon state shared between the agent thread pool and the main poll loop SHALL be both written AND read under the supervisor's single `threading.Lock`. The main loop SHALL NOT read such shared state outside the lock and rely on read atomicity. This covers the rate-limit backoff flag and the per-`_control`-row "entered working" timestamp map.

This closes two races introduced with the thread-pool dispatch:

- the poll loop peeking the backoff flag unlocked could dispatch a fresh batch of
  agents onto an already rate-limited API because a pool thread set the flag in
  the gap between the peek and the dispatch decision;
- the stale-`_control` reclaim reading the "entered working" map unlocked could
  see a half-updated entry while a pool thread populated or popped it, and wrongly
  reclaim a freshly dispatched skill.

#### Scenario: Backoff flag is peeked under the lock

- **GIVEN** the supervisor's poll loop is about to decide whether to dispatch
  claude-driven work this cycle
- **WHEN** it reads the rate-limit backoff flag that pool threads set under the lock
- **THEN** it reads the flag while holding the same lock, so a concurrent set by a
  pool thread cannot be missed

#### Scenario: Reclaim reads the working-since map under the lock

- **GIVEN** a `_control` row currently in `working` and pool threads that populate
  and pop the daemon-owned "entered working" timestamp map under the lock
- **WHEN** the stale-control reclaim computes the row's age from that map
- **THEN** it reads the map entry while holding the same lock before computing the
  age, so the read never races a concurrent populate/pop
