# Spec Delta: skills-catalog

## ADDED Requirements

### Requirement: Operator-invoked catalog top-up (sync)

The system SHALL provide an explicit, operator-invoked way to add catalog skills
that became available *after* the `_skills` tab was first seeded, without
clobbering operator curation. The backend SHALL expose `sync_skills()` which
ensures the catalog tab exists, then appends every `DEFAULT_SKILLS` entry whose
name is **absent** from the current grid (matched by the Skill name in column A),
leaving every existing row — its description and prompt included — untouched, and
SHALL return the list of names it added. It SHALL be exposed on the CLI as
`python -m sheet_agent skills --sync`.

This top-up is **deliberate and manual**. The automatic per-cycle
`ensure_skills_tab` SHALL remain seed-once: it SHALL NOT re-add skills to an
already-seeded tab, so a skill an operator pruned does not silently reappear on
the next poll. Only `sync_skills()` re-adds catalog skills, and only when an
operator explicitly invokes it. The bare `python -m sheet_agent skills` command
SHALL keep its existing seed-once behaviour (it adds nothing to an already-seeded
tab).

#### Scenario: Sync adds only the missing default skills

- **GIVEN** an already-seeded `_skills` tab that is missing the default skill
  `pagespeed` (e.g. seeded before `pagespeed` was added to `DEFAULT_SKILLS`)
- **WHEN** `sync_skills()` runs
- **THEN** a row for `pagespeed` is appended with its default description, prompt
  and `Запуск` trigger
- **AND** the returned list contains `pagespeed`
- **AND** every pre-existing row (and its curated text) is left unchanged

#### Scenario: Sync is a no-op when nothing is missing

- **GIVEN** a `_skills` tab that already contains every `DEFAULT_SKILLS` name
- **WHEN** `sync_skills()` runs
- **THEN** no row is appended and the returned list is empty

#### Scenario: Sync never overwrites a curated prompt

- **GIVEN** a `_skills` tab where an operator has edited the prompt of an existing
  skill
- **WHEN** `sync_skills()` runs
- **THEN** that skill's edited prompt is left exactly as the operator wrote it
  (sync only appends names that are absent)
