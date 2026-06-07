# daemon-robustness Specification

## Purpose
Robustness guarantees of the supervisor: tolerant agent-result parsing and configurable log verbosity.
## Requirements
### Requirement: Agent result parsing tolerates noisy multi-object output

When the streamed `result` event is absent, `agent.run` SHALL still recover the
structured result from the raw transcript by scanning for the LAST well-formed
top-level JSON object, not by matching a single greedy `{...}` span. A transcript
that contains a broken/partial object followed by a valid result object SHALL
resolve to the valid object rather than being reported as "no structured result".

#### Scenario: Last valid JSON object wins over earlier noise

- **GIVEN** a raw transcript containing a malformed `{...}` fragment, then plain
  log noise, then a valid `{"outcome": ...}` object
- **WHEN** the string-path parser runs (no streamed `result` event)
- **THEN** it returns the valid trailing object's structured result

#### Scenario: Genuinely unparseable output yields no result

- **GIVEN** a transcript with no well-formed JSON object at all
- **WHEN** the parser runs
- **THEN** it returns no structured result (the caller treats it as a failure)

### Requirement: Configurable log verbosity

The daemon's log verbosity SHALL be configurable via a `LOG_LEVEL` environment
variable (default `INFO`), so an operator can raise verbosity to `DEBUG` via the
systemd unit without editing code. An unset or unrecognised value SHALL fall back
to `INFO` rather than failing to start.

#### Scenario: LOG_LEVEL raises verbosity

- **GIVEN** `LOG_LEVEL=DEBUG` in the environment
- **WHEN** the logger is constructed
- **THEN** the logger's effective level is `DEBUG`

#### Scenario: Default verbosity

- **GIVEN** no `LOG_LEVEL` set
- **WHEN** the logger is constructed
- **THEN** the logger's effective level is `INFO`

