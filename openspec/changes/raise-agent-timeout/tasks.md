# Tasks: raise-agent-timeout

## 1. Implementation

- [x] 1.1 In `src/sheet_agent/config.py`, change the `agent_timeout` default from
  `_int("AGENT_TIMEOUT", 1800)` to `_int("AGENT_TIMEOUT", 3600)`.

## 2. Verification

- [x] 2.1 Run the offline suite — `./.venv/bin/python -m pytest -q` — green.
- [x] 2.2 `openspec validate raise-agent-timeout --strict` passes.

## 3. Ship

- [x] 3.1 Commit on the working branch, push to `origin`.
- [x] 3.2 Deploy per repo convention (`bash deploy/install.sh` + restart the
  `systemd --user` service).
