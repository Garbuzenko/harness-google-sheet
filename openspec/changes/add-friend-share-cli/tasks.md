# Tasks: add-friend-share-cli

## 1. Shared share-flow method
- [x] 1.1 Add `Orchestrator.share_repos(recipient, repos, autonomy=None, *,
  on_minted=None) -> tuple[str, str, list[str], str]`: validate recipient,
  normalise `repos` (str via `_split_allowlist` or list), reject unbound repos via
  `_tab_bound_to`, resolve + validate autonomy against `AUTONOMY_LEVELS`, mint via
  `_mint_friend_sheet`, fire `on_minted` if given, append the `_friends` row,
  return `(sheet_id, url, repos, autonomy)`.
- [x] 1.2 Refactor `_h_share_repos` to delegate to `share_repos`, passing
  `on_minted=lambda sid, url: orch._set_control(cr.row, result=f"minted {url}")`.
  Behaviour and the result-before-registry ordering unchanged.

## 2. CLI subcommand
- [x] 2.1 Add `share` to the `__main__` command choices plus `--recipient`,
  `--repos`, `--autonomy` arguments.
- [x] 2.2 Implement `_share(cfg, recipient, repos, autonomy)`: validate config,
  build `Orchestrator(cfg)`, call `share_repos`, log + print the URL, return 0;
  on a `ValueError` from validation, log the reason and return non-zero.
- [x] 2.3 Update the module docstring usage block with the new command.

## 3. Tests + validate + ship
- [x] 3.1 Tests: `share_repos` success + each rejection path (no recipient, empty
  repos, unbound repo, bad autonomy) with the Drive seam stubbed; `on_minted`
  fires before the registry append; `_h_share_repos` still records `minted <url>`;
  the `share` CLI happy path + a rejection exit code (MockBackend, no Google).
- [x] 3.2 `openspec validate add-friend-share-cli --strict` passes.
- [x] 3.3 Full offline suite green (`./.venv/bin/python -m pytest -q`).
- [ ] 3.4 Commit, push to origin, deploy (deferred daemon restart — the agent is a
  child of the live daemon and must not restart it synchronously).
- [ ] 3.5 Mint the friend file for the driving request: share
  `wish.example.ru` + `example.ru` with `partner@example.com`
  (via the durable `share_repos` control intent so it survives the live quota
  pressure).
