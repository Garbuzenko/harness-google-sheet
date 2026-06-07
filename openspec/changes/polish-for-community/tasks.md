# Tasks: polish-for-community

## 1. OpenSpec hygiene
- [x] 1.1 Archive the thirteen completed changes (`tidy-and-harden`, `add-repo-chat`,
  `russian-caveman-result-log`, `record-spec-id`, `move-chat-to-paired-tab`,
  `drop-detail-column`, `auto-seed-skills-catalog`, `fixed-row-height`,
  `prune-stale-discovery-repos`, `drop-tries-priority-columns`,
  `drop-product-vision`, `tidy-sheet-russian-skill-launch`, `tidy-chat-tab`).
- [x] 1.2 Fix the `move-chat-to-paired-tab` `repo-chat` delta (rename expressed as
  `REMOVED` old `J/K` requirement + `ADDED` paired-tab requirement) so it applies
  and the published spec matches reality.
- [x] 1.3 Replace every `TBD - created by archiving` spec Purpose with a real
  one-line statement of the capability.

## 2. Fresh code review (verified findings)
- [x] 2.1 Apply the correctness fixes confirmed by the adversarial verify pass:
  two thread-safety races in `orchestrator.py` — the unlocked peek of `_backoff`
  in `run_once` and the unlocked read of `_ctl_working_since` in
  `_reclaim_stale_control` — now read under the supervisor lock.
- [x] 2.2 Apply the confirmed simplifications: drop the dead `self._creds`
  assignment in `sheets.py`, correct the outdated `TEMPLATE_DIR` doc comment in
  `scripts/create_beelink_repo.sh`.

## 3. Simplify + validate + ship
- [x] 3.1 `/simplify` over the resulting diff — consolidated the duplicated
  autonomy-level set into a single `config.AUTONOMY_LEVELS`/`AUTONOMY_CHOICES`
  source of truth reused by `orchestrator.py`.
- [x] 3.2 `openspec validate polish-for-community --strict` passes.
- [x] 3.3 Full offline suite green (`./.venv/bin/python -m pytest -q`).
- [ ] 3.4 Commit, push to origin, deploy (daemon restart via deferred timer — the
  agent is a child of the live daemon and must not restart it synchronously).
