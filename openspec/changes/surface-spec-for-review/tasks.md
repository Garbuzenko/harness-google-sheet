# Tasks: surface-spec-for-review

## 1. Backend note seam
- [x] 1.1 `write_note(title, row, col, text)` on the `Backend` protocol and both
  backends (Google `update_note`; Mock stores it in JSON, with a `get_note` test
  helper). `COL_SPEC_NOTE_MAX` cap constant.

## 2. Attach the spec on spec_ready
- [x] 2.1 `_spec_digest(repo_path, spec_id)`: read `proposal.md` + every spec delta
  from the change folder, joined + capped; '' when absent (best-effort).
- [x] 2.2 On a gated `spec_ready` success, write the digest as a note on the Спека
  cell of the ORIGINATING sheet and point the Log message at it; fall back to the
  id-only message when there is no digest.

## 3. Tests + validate + ship
- [x] 3.1 Tests: digest includes proposal + deltas, is capped, '' when missing; a
  spec_ready run attaches the note on `COL_SPEC` and points the Log at it; missing
  files fall back without a note; suite green.
- [x] 3.2 `openspec validate surface-spec-for-review --strict` passes.
- [x] 3.3 Commit, push, deploy (deferred idle-gated daemon restart).
