# sheet-row-height Specification

## Purpose
Repeating daemon-written data ranges keep a fixed height (CLIP, not WRAP) so many rows stay visible.
## Requirements
### Requirement: Repeating data rows keep a fixed height

The daemon SHALL format the repeating, daemon-written data-row ranges of every
tab it owns with the non-wrapping `CLIP` wrap strategy so that a row's height does
not grow with the length of its content and many rows stay visible at once. This
SHALL apply to the task-grid **Log** column `E`, the chat **transcript** region
(`A4..`/`B4..`), and the `_skills` **Prompt** column `C`. None of these ranges
SHALL use `WRAP`.

Because the formatting helpers (`prettify`, `_prettify_chat_tab`,
`ensure_skills_tab`) run on every poll for every existing repo + chat tab and on
tab creation, this fixed-height formatting SHALL be applied to all tabs, existing
and newly-created, with no operator action, and re-applying it SHALL shrink an
already-grown row back to the default single-line height.

#### Scenario: The task-grid Log column does not wrap

- **GIVEN** the daemon prettifies a repo task tab
- **WHEN** it formats the Log column `E` data rows
- **THEN** their wrap strategy is `CLIP`, not `WRAP`, so a long Log line keeps the
  row at its default single-line height

#### Scenario: The chat transcript does not wrap

- **GIVEN** the daemon prettifies a paired chat tab
- **WHEN** it formats the transcript region `A4..`/`B4..`
- **THEN** the transcript rows use wrap strategy `CLIP`, not `WRAP`

#### Scenario: The skills Prompt column does not wrap

- **GIVEN** the daemon seeds or reformats the `_skills` tab
- **WHEN** it formats the Prompt column `C` data rows
- **THEN** the Prompt rows use wrap strategy `CLIP`, not `WRAP`

### Requirement: Deliberate single-cell reading surfaces stay wrapped

The daemon SHALL keep the `WRAP` strategy on the single, intentionally-tall
reading/writing cells that are not part of a scrolling list: the merged Product
Vision cell (`B2:G2`) on a repo task tab, and the chat **compose box** (`A2`) and
the chat **pinned latest answer** (`B2`) on a paired chat tab. Clipping these
would hide their content and they are single rows, so they do not affect how many
list rows fit on screen.

#### Scenario: Product Vision stays readable

- **GIVEN** the daemon prettifies a repo task tab
- **WHEN** it formats the merged Product Vision cell `B2:G2`
- **THEN** that cell keeps wrap strategy `WRAP` so the full vision text is visible

#### Scenario: The pinned chat answer stays readable

- **GIVEN** the daemon prettifies a paired chat tab
- **WHEN** it formats the compose box `A2` and the pinned answer `B2`
- **THEN** both keep wrap strategy `WRAP`

