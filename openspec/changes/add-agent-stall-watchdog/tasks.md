# Tasks: add-agent-stall-watchdog

## 1. Config

- [x] 1.1 In `src/sheet_agent/config.py` add four env-overridable knobs:
  `agent_stall_timeout` = `_int("AGENT_STALL_TIMEOUT", 1200)`,
  `chat_stall_timeout` = `_int("CHAT_STALL_TIMEOUT", 90)`,
  `bash_default_timeout_ms` = `_int("BASH_DEFAULT_TIMEOUT_MS", 120000)`,
  `bash_max_timeout_ms` = `_int("BASH_MAX_TIMEOUT_MS", 900000)`.

## 2. Layer 1 — bound each tool call

- [x] 2.1 Give `_start_reader` an optional `env: dict | None = None` argument,
  passed through to `subprocess.Popen(..., env=env)` (None keeps inherited env).
- [x] 2.2 Add a helper that returns `{**os.environ, "BASH_DEFAULT_TIMEOUT_MS": …,
  "BASH_MAX_TIMEOUT_MS": …}` from the config, and pass it from both `run()` and
  `chat()` when calling `_start_reader`.

## 3. Layer 2 — stall watchdog

- [x] 3.1 In `run()`'s read loop, track `last_line_at` (monotonic); update it on
  every non-`None` line. If `now - last_line_at > cfg.agent_stall_timeout`, break
  with a `stalled` flag.
- [x] 3.2 On stall: hard-kill the process group, salvage a `result` event if one
  was already emitted (same as the timeout path), else return `outcome="failed"`
  with an `error`/`summary` that names the stall and the elapsed silence.
- [x] 3.3 Mirror the same watchdog in `chat()` using `cfg.chat_stall_timeout`.

## 4. Verification

- [x] 4.1 Add tests (offline, `MockBackend` + fake `CLAUDE_BIN`): a fake claude
  that emits a line then goes silent past the stall window is killed and reported
  as a stall; a fake claude that emits its `result` then lingers silent is
  salvaged (not failed). Assert the BASH_* env is present on the spawned command.
- [x] 4.2 `./.venv/bin/python -m pytest -q` green; `./.venv/bin/ruff check .` clean.
- [x] 4.3 `openspec validate add-agent-stall-watchdog --strict` passes.

## 5. Ship

- [x] 5.1 Mirror the new defaults into the live `.env` (env overrides mask
  `config.py` defaults on this host) — at minimum confirm none are pinned to a
  stale value.
- [x] 5.2 Commit on the working branch, push to `origin`.
- [x] 5.3 Deploy via a **deferred** restart (the daemon cannot restart itself
  synchronously without killing in-flight agents incl. this session): schedule
  `systemd-run --user --on-active=… systemctl --user restart harness-google-sheet`.
