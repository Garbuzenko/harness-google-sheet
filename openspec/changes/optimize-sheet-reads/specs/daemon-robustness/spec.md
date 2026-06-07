# Spec Delta: daemon-robustness

## ADDED Requirements

### Requirement: One batched sheet read per cycle

To stay within the Google Sheets per-minute read quota, the daemon SHALL read each
sheet it polls with a SINGLE batched request per cycle, not one request per tab. The
backend SHALL expose `read_all()` returning every tab's cell grid `{title: grid}`
(the Google backend via one `spreadsheets.get` with `includeGridData` and a
`formattedValue` field mask), and a per-cycle cache (`begin_cycle()`/`end_cycle()`)
from which `list_tab_titles`, `read_tab`, `read_chat_tab`, `read_control` and
`read_friends` are served without further API calls. When no cycle cache is active,
those reads SHALL fall back to live per-tab fetches (backward compatible). Writes
SHALL remain live write-throughs; the cache is read-only and a cycle never re-reads
a tab it wrote, so there is no stale-after-write hazard.

#### Scenario: A poll cycle reads each sheet once

- **GIVEN** a sheet with several repo tabs, chat tabs and a `_control` tab
- **WHEN** the daemon runs one cycle that has begun the cycle cache
- **THEN** exactly one batched `read_all` request is issued for that sheet
- **AND** every per-tab parse (`read_tab`/`read_chat_tab`/`read_control`/
  `read_friends`/`list_tab_titles`) is served from the snapshot with no extra request

#### Scenario: Reads fall back to live when no cycle is active

- **GIVEN** a backend on which `begin_cycle()` has not been called (or `end_cycle()`
  cleared the cache)
- **WHEN** `read_tab` is called
- **THEN** it performs a live per-tab fetch and returns the current tab state

#### Scenario: A failed snapshot skips that sheet, never the daemon

- **GIVEN** a sheet whose `begin_cycle()` snapshot read fails (e.g. a 429 surviving
  retries)
- **WHEN** the cycle runs
- **THEN** that sheet's heavy poll is skipped for the cycle (no per-tab fan-out)
- **AND** the supervisor keeps running and polls the remaining sheets next cycle
