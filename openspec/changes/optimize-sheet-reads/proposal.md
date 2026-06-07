# Change: optimize-sheet-reads

## Why

The supervisor burned the Google Sheets per-minute **read** quota and 429'd
constantly. Each cycle read the sheet **one API request per tab**: `read_tab` per
repo tab, `read_chat_tab` per chat tab, plus `list_tab_titles`, `read_control` and
`read_friends`. With N repo tabs that is `~3 + N_repo + N_chat` reads per sheet per
30s cycle — and Stage 2 multiplied it by every friend sheet. The shared service
account is one "user" (~60 reads/min/user), so a handful of tabs already exceeded
the limit and every cycle hit `429 Quota exceeded`.

The fix is structural, not a knob: the Sheets API can return **every tab's cell
grid in ONE request** (`spreadsheets.get` with `includeGridData` + a
`formattedValue` field mask). Reading each sheet once per cycle and serving all the
per-tab parses from that single snapshot drops reads from `~3 + N_repo + N_chat`
to **1 per sheet per cycle** — a ~10–20x cut that takes the daemon well under the
quota. It also simplifies the read path (one fetch, pure local parsing) and is more
reliable (one retriable call instead of a fan-out where any tab can 429 mid-cycle).

## What Changes

- **New backend snapshot read.** `Backend.read_all()` returns `{tab_title: grid}`
  for every tab. `GoogleBackend.read_all` does it in ONE `spreadsheets.get`
  (`includeGridData=true`, `fields=sheets(properties(title),data(rowData(values(
  formattedValue))))`); `MockBackend.read_all` returns all grids from its JSON.
- **Per-cycle grid cache.** `begin_cycle()` snapshots the whole sheet into an
  in-memory cache; `end_cycle()` clears it. While the cache is set, `list_tab_titles`,
  `read_tab`, `read_chat_tab`, `read_control` and `read_friends` are served from it
  (no API call). With no cache (tests, `once` callers that don't begin a cycle) they
  fall back to the existing live per-tab reads — fully backward compatible.
- **Writes stay live.** `write_cell`/`write_cells`/`heartbeat` are unchanged: they
  write through to the API. The cache is read-only; the cycle never re-reads a tab it
  just wrote, so there is no stale-after-write hazard.
- **Cycle wiring.** `run_once` snapshots the master sheet before its reads and each
  friend sheet before polling it, each `begin_cycle` exception-wrapped: a snapshot
  failure (e.g. a transient 429 surviving the retry) skips that sheet's heavy poll
  for the cycle instead of degrading into the per-tab fan-out, and `end_cycle` always
  runs in a `finally`.

## Non-goals

- Drive-API `modifiedTime` skip-unchanged gating and Drive push-notification
  (`files.watch`) event-driven polling. Both need a broader Drive scope than the SA's
  `drive.file` (which 404s on owner-created files) and extra infra (a public webhook
  endpoint + channel renewal). The single-request snapshot already takes the daemon
  under quota; these are a separate future change if idle reads still need cutting.
- No change to write batching, the poll interval, or any existing invariant
  (OpenSpec-only gate; supervisor never dies; sheet is the durable state; the daemon
  never writes a human-owned cell; chat is read-only).
