# CLAUDE.md

Guidance for Claude Code working in this repo.

## What this is

A supervisor daemon that uses a **Google Sheet as a control plane** for
autonomous coding agents. Each sheet tab binds to a git repo; each row is a task.
The daemon polls the sheet and dispatches short-lived `claude -p` agents that
express every task as an **OpenSpec** change, then implement → test → commit →
push → deploy. Statuses are written back to the row.

## Architecture (one file = one job)

- `config.py` — env-driven config + the fixed sheet layout constants.
- `sheets.py` — `GoogleBackend` (gspread + service account) and `MockBackend`
  (local JSON). Both parse the same fixed grid and bootstrap the schema.
- `repo.py` — resolve a tab's binding to a working dir (path / git-url / name).
- `agent.py` — build & run `claude -p` with `--json-schema` structured output,
  hard timeout, raw-output capture. This is the only place that shells out to claude.
- `orchestrator.py` — the supervisor loop: gate on openspec, dispatch, write back,
  reclaim stale rows, single-instance lock, SIGTERM handling.
- `__main__.py` — `run | once | doctor | bootstrap | repos | skills | share`
  (`skills --sync` tops up the catalog; `share [--recipient] [--repos] [--autonomy]`
  mints + shares a friend file headlessly).

Meta-tabs (`_`-prefixed, never treated as repo tabs): `_repos` (discovery-rebuilt repo
list, B1 dropdown source — the daemon refreshes it each poll cycle from live discovery,
writing only when the set changed, so deleted/`REPO_IGNORE`'d repos drop out without a
manual `repos` CLI run; discovery descends into every non-git folder — a `.gitignore`
there is just workspace-junk rules, not a boundary — so nested checkouts like
`beelink-example.ru/{example.ru,wish.example.ru}` are surfaced, and the
only way to hide a repo is a `REPO_IGNORE` glob), `_control`
(Apps-Script→daemon intent queue), `_skills`
(human-curated catalog of runnable playbooks: `Скилл | Описание | Промпт | Запуск`,
where `Запуск` is the visible `/<скилл>` trigger string), `_friends` (registry of
shared "friend" files for partners: `sheet_id | repos | recipient | autonomy | link`
— the `share_repos` control action mints + shares a new spreadsheet under the SA and
records it here; per-sheet repo allowlist + autonomy default `gated`. The daemon
polls the master sheet AND every registered friend sheet each cycle (Stage 2):
`_poll_sheet(SheetCtx)` carries a per-sheet backend + autonomy + allowlist so every
agent write-back lands on the originating sheet (never the master); a friend tab
outside its allowlist is `blocked`; `_control` runs master-only and a friend
`add_repo`/`create_repo`/`share_repos` intent is rejected — partners can't create
repos. Friend files mint on consumer Gmail only by the OWNER pre-creating + sharing
the spreadsheet to the SA — the SA has no Drive storage and can't create/Drive-share
files; see `openspec/changes/add-friend-sheets/`), and one
`_chat <repo>` per repo (the read-only chat, paired to its repo tab by the shared B1
binding — see the chat invariant below). A skill runs from the sheet TWO ways, both
through the normal OpenSpec-gated agent path: (1) the `▶️ Запустить скилл` menu item
appends a `run_skill` intent `{skill, tab, detail}` (the daemon looks the prompt up in
`_skills`); (2) typing `/<скилл> [доп. контекст]` into a repo task cell (`A`) — the
collect path swaps the cell's text for the catalog skill's prompt and forwards the
trailing text as `detail` (an unknown `/skill` fails that row, pointing at `_skills`).

## Invariants — do not break

- **OpenSpec-only.** Never let a repo without `openspec/` be implemented unless
  `AUTO_OPENSPEC_INIT=true`.
- **The supervisor must never die.** Every cycle/task stays exception-wrapped.
  Don't add a code path that can raise out of `run_forever`.
- **The sheet is the durable state.** Don't add a second source of truth that can
  drift from it. Local `state/` is forensics/locks only, not authority.
- **`_skills` is seed-once + human-curated.** Seed the catalog only when the tab is
  absent/empty (`ensure_skills_tab`); never clobber prunes/prompt edits (contrast
  `_repos`, rebuilt from discovery). A `run_skill` run is materialised as a `_control`
  intent (status/result in E/F), **not** a repo task row — so the "daemon never writes a
  human A/G cell" rule holds. It is dispatched ASYNC through the agent pool (a skill is
  long delivery work; running it inline would block the poll loop) under the same
  per-repo in-flight guard as tasks.
- **Daemon owns columns B/C/D/E/F on a repo task tab.** The only human-owned cells
  are A (Задача) and the B1 binding of an existing tab — never write either. (There is
  no Detail column and no Priority column. The visible grid is `A–F` with **Russian,
  human-friendly headers** `HEADERS = ["Задача","Спека","Статус","Обновлено","Итог",
  "Попытки"]`. **Попытки/Tries (F) is hidden** by `prettify` — daemon-owned durable
  state (dead-lettering / rate-limit backoff) that the human never sees, kept in the
  sheet because the durable-state invariant forbids local `state/`. The Status column
  (C) is **colour-coded** by value via conditional-format rules (`STATUS_COLORS` →
  `_status_cf_requests`, applied idempotently: clear existing rules, then add). A
  `/<скилл>` task cell is resolved to the catalog skill's prompt at collect time (the
  daemon still never writes A). Tasks dispatch in stable sheet order (tab, then row);
  there is no priority ordering. The `run_skill` path still carries its own extra
  context separately.)
- **No Product Vision row.** The repo task tab has NO Product Vision (the old merged
  `B2:F2` cell + `A2`=`VISION` label are gone, along with the `💾 Сохранить vision`
  button and the `sync_vision` handler). Row layout: row 1 is the config row
  (`A1`=REPO_PATH label, `B1`=binding, `C1`=BRANCH, `D1`=branch) with the daemon
  heartbeat in `E1`; `HEADER_ROW`=2; `FIRST_TASK_ROW`=3. The config-row labels are
  Russian: `A1`=`Репозиторий` (was `REPO_PATH`), `C1`=`Ветка` (was `BRANCH`) — only the
  labels changed; the binding/branch are still read by position (`B1`/`D1`). A tab on the
  old English labels/header is relabelled in place by the idempotent
  `_migrate_russianize` migration (which re-`prettify`s so colours land on existing
  tabs); a tab still on the old VISION-row layout has its physical row 2 deleted by the
  idempotent `_migrate_drop_vision_row` migration (a `delete_row` backend seam). `scripts/create_beelink_repo.sh` still takes
  `--vision` to seed a NEW repo's own `docs/strategy/vision.md` at creation — a repo
  artifact, not a sheet feature.
- **Chat lives on a separate PAIRED chat tab, not on the repo tab.** Each repo tab has
  a companion `_chat <repo>` meta-tab (skipped by the repo loop). It is paired by the
  shared `B1` binding, **never** by parsing the title. Layout is compact cols A/B:
  `A1`=`Репозиторий` label, `B1`=binding; `A2`=compose box, `B2`=pinned latest answer;
  `A3`/`B3`=headers; `A4..`/`B4..`=transcript. The config row (row 1: `A1` label + `B1`
  binding) is **hidden** by `_prettify_chat_tab` — daemon plumbing the human never edits,
  mirroring the repo task tab — so the visible chat is just compose box + headers +
  transcript. Hiding is display-only: the binding is still read by position (`B1`) and
  pairing is unaffected. The compose box `A2` is a human INPUT cell
  the daemon only *consumes* (reads, then resets to `CHAT_INPUT_PLACEHOLDER`, never to
  blank — and treats that placeholder as "no question"); the daemon owns the whole
  transcript and the `B2` pin. `add_repo`/`create_repo` create the pair; existing repos
  are migrated on poll (`_ensure_chat_pair`, idempotent by binding). The repo task tab
  no longer carries any chat (its `ensure_schema` stamps nothing in J/K).
- **Chat is read-only.** The chat agent (`agent.chat`) may only Read/Grep/Glob —
  enforced on the `claude` command via `--allowedTools` + `--disallowedTools`, never
  `--dangerously-skip-permissions` (which would defeat it). It must never edit/
  commit/push. Don't route it through the implement path or relax its tool set.
- **Fixed-height data rows.** The repeating, daemon-written ranges — task-grid Log
  (`E`), chat transcript (`A4..`/`B4..`), `_skills` Prompt (`C`) — are formatted
  `wrapStrategy: CLIP`, never `WRAP`, so a long line never grows the row's height
  and many rows stay visible. Only the deliberate single-cell reading surfaces keep
  `WRAP`: the chat compose box (`A2`) and the pinned answer (`B2`) on the chat tab.
  Don't reintroduce `WRAP` on a list/data range.
- Slash commands / skills are unavailable under `claude -p`; drive OpenSpec via
  its CLI inside the agent prompt, not via `/opsx:*`.
- **Two-layer hang harness — keep both layers.** A dispatched agent is bounded by
  MORE than the coarse wall-clock `AGENT_TIMEOUT` (3600s). (1) *Layer 1, in the
  agent:* `agent.run`/`agent.chat` inject `BASH_DEFAULT_TIMEOUT_MS` (2m) +
  `BASH_MAX_TIMEOUT_MS` (15m) into the `claude -p` child env (`_bash_bound_env`), so
  a single bash can't run unbounded. (2) *Layer 2, in the daemon:* the read loop
  tracks `last_line_at` and hard-kills the process group if the output STREAM is
  silent for `AGENT_STALL_TIMEOUT` (20m; chat `CHAT_STALL_TIMEOUT` 90s) — this is
  the only thing that saves a pipe-wedge (a child holding stdout open so `claude`
  itself blocks). Invariant: `AGENT_STALL_TIMEOUT` ≥ `BASH_MAX_TIMEOUT_MS`/1000 — a
  max-length bash is silent on the stream until it returns, so a smaller stall would
  kill a legit deploy. A salvaged `result` still wins over a stall kill. See
  `openspec/changes/add-agent-stall-watchdog/`.

## Dev

```bash
bash deploy/install.sh                 # uses uv (system python3-venv is broken here)
./.venv/bin/ruff check .               # lint (CI runs this first; same excludes via pyproject)
./.venv/bin/python -m pytest -q        # offline tests, no Google/claude needed
./.venv/bin/python -m pytest tests/test_core.py -q          # one file
./.venv/bin/python -m pytest -q -k chat                     # one test by name
SHEET_BACKEND=mock python -m sheet_agent once   # dry-run the whole loop
```

Tests and dry-runs must never call the real Google API or the real `claude`
binary — use `MockBackend` and a fake `CLAUDE_BIN`. CI (`.github/workflows/ci.yml`)
runs `ruff check .` then the offline suite (`SHEET_BACKEND=mock`) on py3.11/3.12 —
no secrets, because the suite never touches a real backend. `ruff` deliberately does
**not** enforce E501 (line length): this codebase uses long, deliberate explanatory
comments — don't reflow them.

## Deploy

Runs as a `systemd --user` service on this host (not Docker — it needs host access
to repos, git, the `claude` CLI and its auth). `Restart=always`, linger enabled.
See README for the service-account + `.env` setup.

**Apps Script side (`appsscript/`).** The `🤖 Supervisor` menu/buttons in the
spreadsheet are a thin Apps Script layer (`Code.gs`) whose ONLY job is to append
intent rows to the `_control` tab (the `sheet-control-queue` contract); the Python
daemon polls `_control` and does all the real work. It's a **separate deploy** —
edit `Code.gs`, commit, then push with `clasp` (manual, needs interactive Google
OAuth; the daemon can't run it). See `appsscript/README.md`.
