# repo-discovery Specification

## Purpose
How the daemon discovers repos for the add-repo dropdown, descending into nested checkouts and pruning stale entries.
## Requirements
### Requirement: Discovery descends into every non-git folder to surface nested checkouts

`repo.discover` SHALL treat any non-git directory as a category folder and descend
into it to find git checkouts nested below, regardless of whether that directory
carries its own `.gitignore`. A `.gitignore` is a workspace-junk convenience (e.g.
ignoring `.env`, dumps, `_migration/`) and SHALL NOT be read as a signal that the
directory is a repo or a boundary. A directory that IS itself a git checkout is added
and never descended into. The only supported way to keep a discovered repo out of the
dropdown is a `REPO_IGNORE` glob.

#### Scenario: Nested checkouts under a .gitignore-bearing category folder are surfaced

- **GIVEN** a search root containing `proj/` which has a `.gitignore` but no `.git`
- **AND** `proj/` contains two git checkouts `proj/a/.git` and `proj/b/.git`
- **WHEN** `discover` runs
- **THEN** both `proj/a` and `proj/b` appear in the result
- **AND** `proj` itself does not appear (it is not a git checkout)

#### Scenario: Deeply nested checkouts in pure category folders are still surfaced

- **GIVEN** a search root containing a category folder `auto/` with NO `.gitignore`
- **AND** a deeply nested checkout `auto/channels/b2c/acme-ai/.git`
- **WHEN** `discover` runs
- **THEN** `auto/channels/b2c/acme-ai` appears in the result

### Requirement: Operator can prune discovered repos with REPO_IGNORE

The configuration SHALL expose `REPO_IGNORE`: a colon-separated list of glob
patterns (empty by default). `repo.discover` SHALL exclude any discovered repo whose
name (its path relative to the search root) matches any pattern via `fnmatch`.
Because the prune lives in discovery rather than in the sheet, it SHALL survive every
`_repos` rebuild.

#### Scenario: A matching glob excludes a repo

- **GIVEN** `REPO_IGNORE` is set to `proj/*:legacy`
- **AND** discovery would otherwise find `proj/a`, `proj/b`, `legacy`, and `keep`
- **WHEN** `discover` runs
- **THEN** the result contains `keep`
- **AND** the result contains none of `proj/a`, `proj/b`, `legacy`

#### Scenario: Empty REPO_IGNORE excludes nothing

- **GIVEN** `REPO_IGNORE` is unset or empty
- **WHEN** `discover` runs
- **THEN** every discovered git checkout is returned

### Requirement: The running daemon keeps the _repos reference tab fresh

The supervisor poll cycle SHALL refresh the `_repos` reference tab (the B1 add-repo
dropdown source) from live discovery, so a repo that has been deleted from disk or
excluded via `REPO_IGNORE` disappears from the dropdown without an operator running
the `repos`/`bootstrap` CLI by hand. The refresh SHALL be quota-friendly: it
rewrites `_repos` (via `ensure_repos_tab`) only when the discovered set has changed
since the previous cycle. Per-tab dropdowns reference the `_repos!$A$2:$A` range, so
refreshing the tab updates every dropdown without rewriting per-tab validation.

The refresh SHALL be best-effort and exception-wrapped: a discovery or write failure
SHALL NOT block repo-task collection and SHALL NOT raise out of the cycle, preserving
the "the supervisor must never die" invariant. Both the `run` loop and the one-shot
`once` command get this behaviour, since both go through the same cycle.

#### Scenario: A deleted repo disappears from _repos after a cycle

- **GIVEN** a backend whose `_repos` tab lists a repo that no longer exists on disk
- **WHEN** a supervisor poll cycle runs and discovery no longer finds that repo
- **THEN** the `_repos` tab no longer lists it
- **AND** it lists exactly the currently-discovered repos

#### Scenario: An unchanged discovered set triggers no rewrite

- **GIVEN** a cycle has already refreshed `_repos` for the current discovered set
- **WHEN** a subsequent cycle runs with the same discovered set
- **THEN** `ensure_repos_tab` is not called again

#### Scenario: A discovery failure never kills the cycle

- **GIVEN** discovery (or `ensure_repos_tab`) raises during the refresh
- **WHEN** a poll cycle runs
- **THEN** the cycle does not raise out (the supervisor never dies)
- **AND** repo-task collection still proceeds

