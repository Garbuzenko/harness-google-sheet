# harness-google-sheet

![harness](docs/images/harness-hero.png)

A **Google Sheet is the control plane** for autonomous coding agents.

Each worksheet (tab) = one git repository. Each row = one human task. A robust,
self-restarting supervisor polls the sheet, turns each task into an **OpenSpec**
change, dispatches a short-lived `claude -p` agent to implement → test → commit →
push → deploy, and writes the status back into the row. New rows are picked up
continuously.

> Policy: **OpenSpec-only.** Repos without an `openspec/` directory are refused
> (or auto-initialised, if you opt in).

```
Google Sheet (control plane)            sheet-agent daemon (systemd, Restart=always)
┌───────────────────────────┐  poll →   ┌─────────────────────────────────────────┐
│ Tab = repository          │ ────────→ │ supervisor loop (never dies)              │
│ A:Task B:Spec C:Status …  │ ← write   │  ├ resolve repo (path / git-url / name)   │
│ row = task                │           │  ├ gate: only repos with openspec/        │
└───────────────────────────┘           │  ├ per task: claude -p (fresh, headless)  │
                                         │  │    openspec new change → implement → ship│
                                         │  └ write Spec / Status / Updated / Log    │
                                         └─────────────────────────────────────────┘
```

Why a dumb supervisor + short agents (not one long agent): a 24/7 session rots
its context and a crash loses everything. Here the supervisor is trivial and
unkillable; each task runs in a fresh, isolated `claude -p` process with a hard
timeout. The **sheet is the durable state**, so a restart resumes exactly where
it left off.

## Sheet layout (the daemon bootstraps this automatically)

| Row | A | B | C | D | E | F (hidden) |
|----|----|----|----|----|----|----|
| 1 | `Репозиторий` | *path or git-url* | `Ветка` | *branch* | *heartbeat (daemon)* | |
| 2 | **Задача** | **Спека** | **Статус** | `Обновлено` | `Итог` | `Попытки` |
| 3+ | *your task* | *(daemon)* | *(daemon)* | *(daemon)* | *(daemon)* | *(daemon, hidden)* |

- **You** own column **A** (Задача) and cell **B1** (the repo binding). (There is no
  Product Vision row, no Detail column and no Priority column — the grid is `A–F`:
  Задача, Спека, Статус, Обновлено, Итог, Попытки. The config-row labels are Russian:
  `A1`=`Репозиторий`, `C1`=`Ветка` — only labels, the binding/branch are still `B1`/`D1`.
  Tasks run in sheet order; there is no priority.)
- **The daemon** owns **B** (OpenSpec change id), **C** (Статус), **D** (Обновлено),
  **E** (Итог), **F** (Попытки — a **hidden** column: daemon-only attempt counter for
  dead-lettering, not human-facing), plus the heartbeat cell **E1**.
- **Статус** flow: *(blank)* / `queued` / `retry` → `working` → `done` /
  `failed` / `blocked`. Set a row to `retry` to re-run a failed task. The Status cell
  is **colour-coded** by value, so state is readable at a glance.
- **Run a skill from the sheet:** type `/<скилл>` (optionally `/<скилл> доп. контекст`)
  into a task cell — the daemon runs that catalog skill's prompt on the tab's repo (the
  trailing text becomes the skill's extra context). The **`_skills`** tab lists every
  skill with its exact `Запуск` trigger string; the `▶️ Запустить скилл` menu still works
  too.
- **Live progress.** While a row is `working`, the **Log (E)** cell shows the
  agent's current stage and an approximate percent plus the autonomy mode, e.g.
  `⏳ implement ~58% (ship)`. It is derived deterministically from the agent's
  tool calls (spec → implement → tests → commit → push → deploy), monotonic, and
  written throttled (on stage change / ≥5-point jump / ≥20 s) so it never spends
  meaningful Sheets quota. The final summary replaces it when the task ends.

`B1` (repo binding) may be an absolute path, a git URL (cloned on demand), or a
bare folder name searched under `REPO_SEARCH_ROOTS`.

## One-time setup

### 1. Google service account (required for the API to write)

The Sheets **API** needs an authenticated identity to write. A service account is
the robust, non-expiring choice for an unattended daemon.

**Console (≈3 min):**
1. https://console.cloud.google.com/ → create/pick a project.
2. APIs & Services → **Enable** the *Google Sheets API*.
3. IAM & Admin → Service Accounts → **Create**. Name it e.g. `sheet-agent`.
4. Open it → **Keys** → Add key → **JSON** → download.
5. Save the JSON on this machine, e.g. `~/.config/sheet-agent-sa.json`.

**You must share the sheet with the service account as Editor.** "Anyone with the
link" defaults to *Viewer*, which lets the daemon read but **not** write — statuses
would silently never persist. Open the sheet → **Share** → paste the `client_email`
from the JSON (e.g. `sheet-agent@<project>.iam.gserviceaccount.com`) → **Editor**.

**Or via gcloud** (if installed):
```bash
gcloud iam service-accounts create sheet-agent --display-name "sheet-agent"
gcloud services enable sheets.googleapis.com
gcloud iam service-accounts keys create ~/.config/sheet-agent-sa.json \
  --iam-account "sheet-agent@$(gcloud config get-value project).iam.gserviceaccount.com"
```

### 2. `.env`

Create `.env` in the project root (gitignored):

```dotenv
SHEET_BACKEND=google
SHEET_ID=your-google-sheet-id   # the long id from the sheet URL: /spreadsheets/d/<THIS>/edit
GOOGLE_SA_JSON=$HOME/.config/sheet-agent-sa.json

# Agent behaviour
AUTONOMY=ship                 # spec | code | ship | gated
CLAUDE_MODEL=claude-opus-4-8     # most capable; used only when a task runs
AGENT_TIMEOUT=1800            # per-task hard timeout (s)
POLL_INTERVAL=30             # sheet poll cadence (s)
MAX_CONCURRENT_AGENTS=2      # agents in parallel across distinct repos (1 = serial)
MAX_ATTEMPTS=3               # dead-letter a row after N dispatches (retry/approved reset)
RUNS_KEEP=300                # cap raw agent-output dumps under state/runs/ (0 = unbounded)

# Repo resolution
REPO_SEARCH_ROOTS=$HOME/projects
CLONE_ROOT=$HOME/projects
# REPO_IGNORE=legacy/*:*-archived  # colon-separated globs (matched on the repo's name
#                                  # relative to its root) to prune неактуальные repos
#                                  # from the ➕ Добавить репо dropdown. Survives rebuilds.
# AUTO_OPENSPEC_INIT=false    # set true to let the agent `openspec init` missing repos

# Sharing repos with partners (friend sheets)
# OWNER_EMAIL=you@example.com       # every minted friend file is shared back to you
# FRIEND_DEFAULT_AUTONOMY=gated     # spec|code|ship|gated — default autonomy of a new
#                                   # friend file (gated = partner files a task → agent
#                                   # writes the spec + branch, you review & deploy)
```

### 3. Install & run

```bash
bash deploy/install.sh                          # venv + deps + systemd unit + linger
./.venv/bin/python -m sheet_agent doctor        # validate config + connectivity
systemctl --user enable --now harness-google-sheet
journalctl --user -u harness-google-sheet -f    # live logs
```

## CLI

```bash
python -m sheet_agent doctor      # check config + list tabs/rows, no work done
python -m sheet_agent bootstrap   # write the schema headers onto every tab (+ _repos/_skills)
python -m sheet_agent repos       # (re)build the _repos reference tab + B1 dropdowns
python -m sheet_agent skills      # create + seed the _skills catalog tab (seed-once)
python -m sheet_agent once        # run exactly one poll cycle, then exit
python -m sheet_agent run         # the supervisor loop (what systemd runs)
```

## Skills catalog (`_skills`) — run a playbook from the menu

Besides filing tasks, you can run a **catalog skill** against the active repo tab from
the `🤖 Supervisor` ▸ **▶️ Запустить скилл** menu. The catalog lives on the `_skills`
meta-tab (`Skill | Description | Prompt`), seeded once with a broad set of delivery
playbooks spanning code quality, robustness, security, performance, docs, engineering
hygiene and running-product UX (`autopilot`, `add-tests`, `harden`, `simplify`,
`refresh-docs`, `security-pass`, `perf-pass`, `code-review`, `ux-loop-fix`, `pagespeed`,
`accessibility`, `observability`, `ci-setup`, `type-coverage`, `lint-format`,
`resilience-retries`, `input-validation`, `api-docs`, `onboarding-docs`, …) — prune what
you don't want; your edits and the prompts you change
are never clobbered (the seed runs only when the tab is empty). Picking a skill appends a
`run_skill` intent to `_control`; the daemon looks up the skill's **Prompt** (column C)
and runs it against the tab's repo through the same OpenSpec-gated agent path as a task,
reporting `working`/`done`/`error` back into the `_control` row.

## Autonomy levels (`AUTONOMY`)

- `spec` — only create & validate the OpenSpec change. Safest. A human implements.
- `code` — spec + implement + tests + commit on `agent/<change-id>`. No push/deploy.
- `ship` — full pipeline: spec → implement → tests → commit → push → deploy
  (`bash deploy/deploy.sh` if present). Maximum autonomy.
- `gated` — two-stage review. The agent writes the spec, then parks the row in
  `spec_ready`. A human reads `openspec/changes/<id>/` and sets the status to
  `approved`; the daemon then implements and ships the **approved** spec. The safe
  default for prod-deploying repos — code is never written without a human OK.

## Sharing repos with partners (`_friends`) — **Stage 1**

Hand a business partner a *scoped* control plane: a separate Google file exposing
only the repos you choose. From `🤖 Supervisor ▸ 📤 Поделиться репо` pick a recipient
e-mail and one or more repos; the daemon mints a brand-new spreadsheet under the
service account, shares it with you (`OWNER_EMAIL`) and the partner, seeds it with a
bound tab per shared repo, and records the file in the **`_friends`** registry tab
(`sheet_id | repos | recipient | autonomy | link`). A friend file defaults to
`gated` autonomy (`FRIEND_DEFAULT_AUTONOMY`) — partner files a task, the agent writes
the OpenSpec spec + branch and parks it for you to review and deploy; raise a trusted
partner's file to `ship` by editing its `autonomy` cell in `_friends`.

> **Staging.** This is Stage 1: the registry, the share/mint flow and the per-sheet
> policy model (repo allowlist + autonomy). Stage 2 wires the live multi-sheet poll
> loop (the daemon also polling every registered friend file, enforcing its allowlist,
> applying its autonomy, and ignoring any repo-creation intent from a friend file). A
> freshly minted friend file has no bound Apps Script and no `_control` tab, so it
> already cannot create repos. See `openspec/changes/add-friend-sheets/`.

## Concurrency

Agents for **distinct** repos run in parallel (up to `MAX_CONCURRENT_AGENTS`); two
agents never run in the same working dir (they would race on git). A long task in
one repo no longer blocks the others. The supervisor stays single-instance (file
lock); only the per-repo agent dispatch is parallel.

## Attempts & dead-lettering

The hidden column `F (Tries)` counts dispatches. After `MAX_ATTEMPTS` the row is
dead-lettered to `failed` so a poisoned or quota-starved task can't loop forever on
the metered Agent SDK pool. Setting status `retry` (or `approved`) resets the counter.
The column is hidden (daemon-only state); to re-run, set status `retry` rather than
editing it. Tasks dispatch in sheet order — there is no priority.

## Robustness ("неубиваемость")

- systemd `Restart=always`, `RestartSec=5`, `StartLimitIntervalSec=0` (never gives
  up), `loginctl enable-linger` (survives logout/reboot).
- Every cycle and every task is exception-wrapped — one failure can't kill the loop.
- Each agent runs in its own process with a hard `AGENT_TIMEOUT`; a hung agent is
  killed, the row marked `failed`, the daemon continues.
- Single-instance file lock — two daemons can't fight over the sheet.
- Rows stuck in `working` after a crash are reclaimed to `queued` after a grace
  period (`2 × AGENT_TIMEOUT`).
- Sheets API calls retry with exponential backoff.
- Raw agent output for every run is kept under `state/runs/` for forensics.

## Dry-run without Google

```bash
SHEET_BACKEND=mock python -m sheet_agent once
```
Uses a local JSON file (`state/mock_sheet.json`) that mimics the sheet — the path
used by the test suite. Run tests with `./.venv/bin/python -m pytest -q`.

## Note on `create_repo` (opinionated extra)

The `create_repo` flow (`scripts/create_beelink_repo.sh` + the `create-beelink-repo`
skill) is the author's own provisioning recipe: it names repos `beelink-<name>`,
registers them against a private internal **deployer** API, scaffolds a Docker
compose file and pushes to a fixed GitHub owner. It is included as a worked example
of "no LLM in the irreversible path", but it is wired to a specific home-server
setup — adapt `scripts/create_beelink_repo.sh` (or drop the feature) before using it
elsewhere. The core harness (sheet → agent → OpenSpec → ship), the chat tab, the
skills catalog and friend sheets are all infra-agnostic.

## License

MIT — see [LICENSE](LICENSE).
