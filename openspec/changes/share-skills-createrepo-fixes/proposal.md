# Change: share-skills-createrepo-fixes

## Why

Three concrete operator papercuts surfaced in one work session, all on the
control-plane's admin surface:

1. **Carve a repo into its own friend file for *colleagues* (no single
   recipient).** The owner wants to hand `beelink-payroll` to a group to
   "play with" — there is no one named partner to share to at mint time. Today
   `share_repos` / the `share` CLI **require** a recipient e-mail and refuse a
   blank one, so the only way to mint the file head-less is to invent a throw-away
   address.

2. **A `/<скилл>` typed into a task cell fails for skills the catalog doesn't
   list.** `/pagespeed` failed with *"неизвестный скилл /pagespeed"* even though
   `pagespeed` is in the seeded `DEFAULT_SKILLS`. The `_skills` tab is **seed-once**
   (so operator prunes/edits survive), which means skills added to the default
   catalog *after* the tab was first seeded never reach an existing sheet. There is
   no way to top up an already-seeded catalog with newly-available skills short of
   deleting the whole tab (which would clobber curation).

3. **`create_repo` rejects domain-style names.** Clicking "создать репо" for
   `foo.ru` failed: `create_beelink_repo.sh` validates the bare name against
   `^[a-z0-9][a-z0-9-]*$`, which forbids the dot — so nothing was created and the
   repo never appeared in the sheet. Yet domain-style beelink repos with dots
   already exist (e.g. `beelink-example.ru`), so a dotted name is legitimate;
   the validation is simply too strict.

## What Changes

- **Optional recipient in the share flow.** `Orchestrator.share_repos` accepts a
  blank recipient: validation becomes "recipient OR `OWNER_EMAIL` present" so a
  file only the service account could reach is never minted. When blank, the
  recipient-share is skipped (the best-effort owner-share becomes the only human
  grant), the file title is derived from the repo allowlist, and the `_friends`
  row records a blank recipient. The `share` CLI's `--recipient` becomes optional.
  No "anyone with the link" / public mode is added — a friend file dispatches
  autonomous agents against real repos and must never be writable by an
  unauthenticated link.

- **Operator-invoked catalog top-up (`skills --sync`).** A new, **explicit**
  `sync_skills()` backend action appends any `DEFAULT_SKILLS` entries missing from
  the existing `_skills` tab (matched by name), leaving every existing row — and
  its curated description/prompt — untouched. Exposed as `python -m sheet_agent
  skills --sync`. This is a deliberate manual top-up; the *automatic* per-cycle
  `ensure_skills_tab` stays seed-once (it never re-adds a pruned skill on its own),
  so the "prunes survive" invariant is preserved.

- **`create_repo` accepts dotted names.** `scripts/create_beelink_repo.sh` relaxes
  the name validation to `^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$` — lower-case
  alphanumerics with internal dots/hyphens, never a leading/trailing separator — so
  `foo.ru` and `example.ru` are accepted while `Foo_Bar`, `-leading`,
  `UPPER`, `a/b` and `has space` are still rejected.

## Non-goals

- No "anyone with the link" / public sharing — explicitly rejected above.
- No change to the `_friends` registry schema, the per-sheet policy model, the
  autonomy levels, or the Apps-Script menu.
- No Stage 2 work (the live multi-sheet poll loop that operates friend files).
- The automatic per-cycle skills seed stays **seed-once** — `skills --sync` is the
  only path that re-adds catalog skills, and only when explicitly invoked.
- No change to the single-sheet invariants: OpenSpec-only gate; the supervisor
  must never die; the sheet is the durable state; the daemon never writes a
  human-owned cell; chat is read-only.
