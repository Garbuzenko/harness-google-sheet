# Change: surface-spec-for-review

## Why

Under `gated` autonomy the agent writes the spec, parks the row `spec_ready`, and
the human flips the status to `approved` to trigger the deploy. But the row only
showed the **change id** (column B `Спека`) — the human had to leave the sheet and
open `openspec/changes/<id>/` on the server to actually read what they were
approving. Approving blind on an id defeats the point of the gate.

## What Changes

- When a gated spec is ready, the daemon reads the just-written OpenSpec change from
  disk (`proposal.md` + every `specs/**/spec.md` delta) and attaches it as a **cell
  NOTE on the Спека cell** (`COL_SPEC`). The change id stays in the cell; its full
  human-readable body sits in the note — read on hover/click, capped at
  `COL_SPEC_NOTE_MAX`, and (being a note) never grows the row height.
- The `Итог`/Log message changes to point at the note: "открой примечание ячейки
  «Спека», прочитай, потом статус approved".
- New backend seam `write_note(title, row, col, text)` (Google `update_note`; Mock
  stores it in JSON). The note is routed to the ORIGINATING sheet's backend, so a
  friend-sheet spec is reviewable on the friend sheet too.
- Best-effort: if the spec files can't be read, the row falls back to the previous
  id-only message — never a crash, never a blocked task.

## Non-goals

- No change to the gated flow itself (still `spec_ready` → human `approved` → deploy)
  or to any column's ownership. The note is additional review context, not a new
  control surface; the human still approves by editing the Status cell.
- No rendering of the spec into the visible grid (it would blow row heights and the
  CLIP/fixed-height invariant); the cell note is the deliberate reading surface.
