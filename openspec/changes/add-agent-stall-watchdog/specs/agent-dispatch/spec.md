# Spec Delta: agent-dispatch

## ADDED Requirements

### Requirement: Bounded tool-call duration

The supervisor SHALL bound the duration of any single bash/shell tool call a
dispatched agent runs, by injecting `BASH_DEFAULT_TIMEOUT_MS` (default **120000**)
and `BASH_MAX_TIMEOUT_MS` (default **900000**) into the `claude -p` child
environment for BOTH the implement path (`agent.run`) and the read-only chat path
(`agent.chat`). Both values SHALL be overridable via the same-named environment
variables. An agent SHALL NOT be able to run a single bash call longer than the
max ceiling.

#### Scenario: Defaults injected into the agent environment

- **GIVEN** no `BASH_DEFAULT_TIMEOUT_MS` / `BASH_MAX_TIMEOUT_MS` set
- **WHEN** the daemon spawns a dispatch agent
- **THEN** the child environment carries `BASH_DEFAULT_TIMEOUT_MS=120000` and
  `BASH_MAX_TIMEOUT_MS=900000`

#### Scenario: Environment overrides the tool-call bounds

- **GIVEN** `BASH_MAX_TIMEOUT_MS=300000` is set in the environment
- **WHEN** the daemon spawns a dispatch agent
- **THEN** the child environment carries `BASH_MAX_TIMEOUT_MS=300000`

### Requirement: Stall watchdog on stream silence

The supervisor SHALL monitor the agent's output stream for silence and treat a
silent agent as failed without waiting for the coarse wall-clock `AGENT_TIMEOUT`.
For the implement path, if no stream line arrives for `AGENT_STALL_TIMEOUT`
(default **1200** seconds) the agent process group SHALL be hard-killed. For the
read-only chat path the same watchdog SHALL apply with `CHAT_STALL_TIMEOUT`
(default **90** seconds). Both values SHALL be environment-overridable.
`AGENT_STALL_TIMEOUT` SHALL be no smaller than `BASH_MAX_TIMEOUT_MS` (expressed in
seconds) so that a legitimate maximum-length tool call — which is silent on the
stream until it returns — cannot trip the watchdog.

#### Scenario: Silent agent is killed before the wall-clock timeout

- **GIVEN** a dispatched agent that emits an initial line then sends nothing for
  longer than `AGENT_STALL_TIMEOUT`, while `AGENT_TIMEOUT` is far larger
- **WHEN** the read loop runs
- **THEN** the process group is hard-killed and the result is `outcome="failed"`
  with an `error` that names the stall (not a generic wall-clock timeout)

#### Scenario: An emitted result is salvaged even on stall

- **GIVEN** an agent that emits its terminal `result` event and then lingers
  silent (a child holding the stdout pipe open) past `AGENT_STALL_TIMEOUT`
- **WHEN** the watchdog fires and hard-kills the group
- **THEN** the already-emitted structured result is salvaged rather than recorded
  as a stall failure

#### Scenario: Default stall timeouts

- **GIVEN** no `AGENT_STALL_TIMEOUT` / `CHAT_STALL_TIMEOUT` set
- **WHEN** the daemon config is constructed
- **THEN** `agent_stall_timeout` is `1200` and `chat_stall_timeout` is `90`
