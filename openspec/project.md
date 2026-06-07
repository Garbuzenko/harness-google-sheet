# Project Context

## Purpose
`harness-google-sheet` (sheet-agent) — a supervisor daemon that uses a **Google
Sheet as the control plane** for autonomous coding agents. Each sheet tab binds
to a git repo; each row is a task. The daemon polls the sheet and dispatches
short-lived `claude -p` agents that express every task as an **OpenSpec** change,
then implement → test → commit → push → deploy, writing status back to the row.

## Tech Stack
- Python 3, `uv` venv (system `python3-venv` is broken on this host)
- `gspread` + Google service account (sheets), file-lock single-instance
- Runs as a `systemd --user` service (`Restart=always`, linger) — not Docker
- `pytest` (offline; MockBackend + fake `CLAUDE_BIN`, never real Google/claude)

## Project Conventions

### Code Style
- One file = one job (`config`, `sheets`, `repo`, `agent`, `orchestrator`,
  `__main__`). Keep that separation.

### Conventions
- **Spec-driven via OpenSpec — mandatory.** A change to this repo's behaviour is
  first an OpenSpec change in `openspec/changes/`, validated with
  `openspec validate --strict`, then code. The supervisor enforces this on the
  repos it operates on; it now holds itself to the same rule.
- **Invariants (do not break):** OpenSpec-only gate; the supervisor must never
  die (every cycle/task exception-wrapped); the sheet is the durable state;
  daemon owns sheet columns B/C/E/F only (never A/D). See `CLAUDE.md`.
- Slash commands / skills are unavailable under `claude -p` — drive OpenSpec via
  its CLI, not `/opsx:*`.

### Testing Strategy
- `./.venv/bin/python -m pytest -q` — offline, must be green before commit.
- `SHEET_BACKEND=mock python -m sheet_agent once` — dry-run the whole loop.

### Deploy
- `bash deploy/install.sh` (idempotent: venv + deps + systemd unit + linger),
  then `systemctl --user enable --now harness-google-sheet`.
