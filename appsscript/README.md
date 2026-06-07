# appsscript/ — 🤖 Supervisor button layer (clasp)

Thin Apps Script layer over the sheet-agent control plane. It runs in Google's
cloud and can touch ONLY the spreadsheet — its sole job is to **append intent rows
to the `_control` tab** (the `openspec/specs/sheet-control-queue` contract). The Python daemon on
the server polls `_control` and does all the real work.

**Git is the source of truth.** Edit `Code.gs` here, commit, then deploy with
`clasp` (a separate, manual step that needs interactive Google OAuth — it cannot be
run by the daemon).

## Layout

- `Code.gs` — `onOpen` builds the `🤖 Supervisor` menu (3 items) + the 3 dialogs.
- `appsscript.json` — Apps Script manifest (V8 runtime).
- `.clasp.json` — clasp config. Replace `scriptId` after `clasp create`/`clone`.

## Deploy (manual, one-time OAuth)

```bash
npm i -g @google/clasp
clasp login                                  # interactive OAuth in a browser
# first time only — create the bound script and capture its scriptId:
#   clasp create --type sheets --title "sheet-agent supervisor" --rootDir appsscript
# then keep .clasp.json under git and push source from git:
clasp push -f                                # from appsscript/ (rootDir=".")
```

After `clasp push`, reload the spreadsheet — the `🤖 Supervisor` menu appears.

## Contract (`_control`, §4.1)

| Col | Field   | Writer       | Notes                                  |
|-----|---------|--------------|----------------------------------------|
| A   | id      | Apps Script  | `<ts>-<rand>` idempotency key           |
| B   | ts      | Apps Script  | ISO click time                          |
| C   | action  | Apps Script  | `add_repo` \| `create_repo` \| `run_skill` |
| D   | args    | Apps Script  | JSON                                    |
| E   | status  | Apps Script  | set to `pending` on append              |
| F   | result  | **daemon**   | dialogs NEVER write this                |
