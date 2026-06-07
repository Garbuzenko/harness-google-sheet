# Spec Delta: agent-dispatch

## ADDED Requirements

### Requirement: Default agent dispatch timeout

The supervisor SHALL bound every dispatched implement-and-ship agent run by a hard
timeout whose default is **3600 seconds**. The value SHALL be overridable via the
`AGENT_TIMEOUT` environment variable; absent that variable the daemon SHALL use
3600 seconds. On expiry the agent process group SHALL be hard-killed (an
already-emitted structured result is still salvaged rather than recorded as a
timeout failure).

#### Scenario: Default timeout is 3600 seconds

- **GIVEN** no `AGENT_TIMEOUT` environment variable is set
- **WHEN** the daemon config is constructed
- **THEN** `agent_timeout` is `3600`

#### Scenario: Environment variable overrides the default

- **GIVEN** `AGENT_TIMEOUT=900` is set in the environment
- **WHEN** the daemon config is constructed
- **THEN** `agent_timeout` is `900`
