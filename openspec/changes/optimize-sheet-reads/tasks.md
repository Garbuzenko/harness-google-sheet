# Tasks: optimize-sheet-reads

## 1. Backend snapshot + per-cycle cache
- [x] 1.1 Add `read_all() -> dict[str, list[list[str]]]`, `begin_cycle()`,
  `end_cycle()` to the `Backend` protocol and both backends.
- [x] 1.2 `GoogleBackend.read_all`: ONE `spreadsheets.get` with `includeGridData` +
  a `formattedValue` field mask, parsed into `{title: grid}` (retriable).
- [x] 1.3 Per-cycle `_grid_cache`; `_grid(title)` returns the cached grid when set,
  else the live per-tab read. Route `list_tab_titles`, `read_tab`, `read_chat_tab`,
  `read_control`, `read_friends` through it on both backends.
- [x] 1.4 `MockBackend.read_all`/`begin_cycle`/`end_cycle` mirror the contract from
  its JSON so the cache semantics are testable offline.

## 2. Cycle wiring
- [x] 2.1 `run_once` snapshots the master sheet (begin/end in a `finally`) before its
  reads; a failed snapshot skips the master poll for that cycle (no per-tab fan-out).
- [x] 2.2 Each friend sheet is snapshotted before `_poll_sheet`; a failed friend
  snapshot skips that friend, never the cycle.

## 3. Tests + validate + ship
- [x] 3.1 Tests: a cycle issues ONE `read_all` per sheet (no per-tab reads); reads
  are served from the snapshot (mutating the store mid-cycle is not seen until the
  next `begin_cycle`); fallback to live reads when no cycle is begun; suite green.
- [x] 3.2 `openspec validate optimize-sheet-reads --strict` passes.
- [x] 3.3 Commit, push, deploy (deferred idle-gated daemon restart).
