"""Offline tests for friend sheets: the `_friends` registry, the per-sheet policy
model, and the `share_repos` control handler (Drive seam stubbed).

No Google, no claude. Run: pytest -q
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sheet_agent import config as C
from sheet_agent import orchestrator as orch_mod
from sheet_agent.orchestrator import (
    Orchestrator, friend_autonomy, friend_repo_allowed, _h_share_repos,
)
from sheet_agent.sheets import (
    Friend, MockBackend, parse_friends_grid, _split_allowlist,
)


def _mock_orch(tmp_path: Path, **kw) -> Orchestrator:
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"), **kw)
    return Orchestrator(cfg)


def _bind_master_repo(be: MockBackend, binding: str) -> None:
    """Create a master repo tab bound (B1) to `binding`, like add_repo would."""
    title = binding
    be.create_tab(title)
    be.write_cell(title, C.CONFIG_ROW, C.COL_REPO_BINDING, binding)


class _Ctl:
    """Minimal stand-in for a ControlRow (the handler only reads .row)."""
    def __init__(self, row: int = 2):
        self.row = row


# --- AC: constants ---------------------------------------------------------
def test_friends_constants():
    assert C.FRIENDS_TAB == "_friends"
    assert C.FRIENDS_TAB.startswith(C.META_PREFIX)
    assert C.FRIENDS_HEADERS == ["sheet_id", "repos", "recipient", "autonomy", "link"]
    assert C.ACTION_SHARE_REPOS == "share_repos"


# --- AC: allowlist splitting ------------------------------------------------
def test_split_allowlist_newline_and_comma():
    assert _split_allowlist("a\nb") == ["a", "b"]
    assert _split_allowlist("a, b ,c") == ["a", "b", "c"]
    assert _split_allowlist("a\n\nb,,a") == ["a", "b"]   # blanks + dups dropped
    assert _split_allowlist("") == []


# --- AC: registry parsing ---------------------------------------------------
def test_parse_friends_grid():
    grid = [
        C.FRIENDS_HEADERS,
        ["FID1", "repo-a\nrepo-b", "p@x.com", "gated", "http://l1"],
        ["FID2", "repo-c", "q@x.com", "", "http://l2"],   # blank autonomy → gated
        ["", "", "", "", ""],                              # blank row skipped
    ]
    friends = parse_friends_grid(grid)
    assert len(friends) == 2
    assert friends[0] == Friend(
        sheet_id="FID1", repos=["repo-a", "repo-b"], recipient="p@x.com",
        autonomy="gated", link="http://l1")
    assert friends[1].autonomy == "gated"   # defaulted from blank


# --- AC: registry backend round-trip ---------------------------------------
def test_mock_friends_schema_idempotent_and_append(tmp_path: Path):
    be = MockBackend(str(tmp_path / "m.json"))
    be.ensure_friends_schema()
    be.ensure_friends_schema()   # idempotent — no duplicate header, no rows
    assert be.read_friends() == []
    be.append_friend("FID1", ["repo-a", "repo-b"], "p@x.com", "gated", "http://l")
    friends = be.read_friends()
    assert len(friends) == 1
    f = friends[0]
    assert f.sheet_id == "FID1" and f.repos == ["repo-a", "repo-b"]
    assert f.recipient == "p@x.com" and f.autonomy == "gated"
    # A second append adds a second row, not a clobber.
    be.append_friend("FID2", ["repo-c"], "q@x.com", "ship", "http://l2")
    assert [x.sheet_id for x in be.read_friends()] == ["FID1", "FID2"]


# --- AC: per-sheet policy model --------------------------------------------
def test_friend_repo_allowed():
    f = Friend(repos=["repo-a", "/home/u/projects/repo-b"])
    assert friend_repo_allowed(f, "repo-a") is True
    assert friend_repo_allowed(f, "/home/u/projects/repo-b") is True
    # bare name <-> full path both directions
    assert friend_repo_allowed(f, "repo-b") is True
    assert friend_repo_allowed(f, "repo-c") is False
    assert friend_repo_allowed(f, "") is False
    assert friend_repo_allowed(Friend(repos=[]), "repo-a") is False   # deny by default


def test_friend_autonomy_default_and_override(tmp_path: Path):
    cfg = C.Config(backend="mock", mock_path=str(tmp_path / "m.json"))
    assert friend_autonomy(Friend(autonomy=""), cfg) == "gated"       # default
    assert friend_autonomy(Friend(autonomy="ship"), cfg) == "ship"    # owner raised
    assert friend_autonomy(Friend(autonomy="bogus"), cfg) == cfg.friend_default_autonomy


# --- AC: share_repos handler — success --------------------------------------
def test_share_repos_mints_registers_and_links(tmp_path: Path):
    orch = _mock_orch(tmp_path, owner_email="owner@x.com")
    _bind_master_repo(orch.backend, "repo-a")

    calls = []
    orch._mint_friend_sheet = lambda recipient, repos, autonomy: (
        calls.append((recipient, tuple(repos), autonomy))
        or ("FID-NEW", "https://docs.google.com/spreadsheets/d/FID-NEW/edit"))

    res = _h_share_repos(orch, _Ctl(), {"recipient": "p@x.com", "repos": ["repo-a"]})
    assert "FID-NEW" in res and "p@x.com" in res
    # the Drive seam was called once with the validated repos + default autonomy
    assert calls == [("p@x.com", ("repo-a",), "gated")]
    friends = orch.backend.read_friends()
    assert len(friends) == 1
    assert friends[0].sheet_id == "FID-NEW"
    assert friends[0].repos == ["repo-a"]
    assert friends[0].recipient == "p@x.com"
    assert friends[0].autonomy == "gated"


def test_share_repos_autonomy_override(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    _bind_master_repo(orch.backend, "repo-a")
    orch._mint_friend_sheet = lambda recipient, repos, autonomy: ("F", "http://l")
    _h_share_repos(orch, _Ctl(), {"recipient": "p@x.com", "repos": ["repo-a"],
                                  "autonomy": "ship"})
    assert orch.backend.read_friends()[0].autonomy == "ship"


# --- AC: share_repos handler — rejection paths ------------------------------
@pytest.mark.parametrize("args", [
    {"recipient": "", "repos": ["repo-a"]},          # no recipient
    {"recipient": "p@x.com", "repos": []},           # empty repos
    {"recipient": "p@x.com"},                        # missing repos
    {"recipient": "p@x.com", "repos": ["nope"]},     # repo not bound on master
    {"recipient": "p@x.com", "repos": ["repo-a"], "autonomy": "wild"},  # bad autonomy
])
def test_share_repos_rejects_bad_args(tmp_path: Path, args):
    # owner_email forced blank so the empty-recipient case is a rejection (with an
    # owner configured a blank recipient is the valid "owner-distributed" path).
    orch = _mock_orch(tmp_path, owner_email="")
    _bind_master_repo(orch.backend, "repo-a")
    minted = []
    orch._mint_friend_sheet = lambda *a, **k: minted.append(1) or ("F", "u")
    with pytest.raises((ValueError, TypeError)):
        _h_share_repos(orch, _Ctl(), args)
    # nothing minted, nothing registered
    assert minted == []
    assert orch.backend.read_friends() == []


# --- AC: handler is registered for the dispatcher ---------------------------
def test_share_repos_handler_registered():
    assert orch_mod.CONTROL_HANDLERS.get("share_repos") is orch_mod._h_share_repos


# --- AC: shared share_repos method — on_minted ordering ---------------------
def test_share_repos_method_on_minted_before_registry(tmp_path: Path):
    orch = _mock_orch(tmp_path)
    _bind_master_repo(orch.backend, "repo-a")
    events: list[str] = []
    orch._mint_friend_sheet = lambda r, repos, a: (
        events.append("mint") or ("FID", "http://u"))
    real_append = orch.backend.append_friend
    orch.backend.append_friend = (
        lambda *a, **k: events.append("append") or real_append(*a, **k))

    sid, url, repos, autonomy = orch.share_repos(
        "p@x.com", ["repo-a"],
        on_minted=lambda s, u: events.append(f"minted:{s}:{u}"))

    assert (sid, url, repos, autonomy) == ("FID", "http://u", ["repo-a"], "gated")
    # mint, then the callback, then the registry write — never the other order
    assert events == ["mint", "minted:FID:http://u", "append"]


def test_share_repos_method_accepts_string_repos(tmp_path: Path):
    """The CLI passes `--repos` as a raw string; the method splits it itself."""
    orch = _mock_orch(tmp_path)
    _bind_master_repo(orch.backend, "repo-a")
    _bind_master_repo(orch.backend, "repo-b")
    orch._mint_friend_sheet = lambda r, repos, a: ("F", "u")
    _, _, repos, _ = orch.share_repos("p@x.com", "repo-a, repo-b")
    assert repos == ["repo-a", "repo-b"]


# --- AC: `share` CLI subcommand ---------------------------------------------
def test_share_cli_happy_path(tmp_path: Path, monkeypatch, capsys):
    from sheet_agent.__main__ import _share

    path = str(tmp_path / "m.json")
    _bind_master_repo(MockBackend(path), "repo-a")
    monkeypatch.setattr(
        Orchestrator, "_mint_friend_sheet",
        lambda self, r, repos, a: ("FID-CLI", "https://docs.google.com/d/FID-CLI"))

    cfg = C.Config(backend="mock", mock_path=path)
    rc = _share(cfg, "p@x.com", "repo-a", None)

    assert rc == 0
    assert "FID-CLI" in capsys.readouterr().out
    friends = MockBackend(path).read_friends()
    assert len(friends) == 1
    assert friends[0].recipient == "p@x.com"
    assert friends[0].repos == ["repo-a"]
    assert friends[0].autonomy == "gated"


# --- AC: optional recipient (owner-distributed share) -----------------------
def test_share_repos_blank_recipient_owner_distributed(tmp_path: Path):
    """A blank recipient is accepted when OWNER_EMAIL is set: the file is minted
    and the `_friends` row records an EMPTY recipient (owner distributes it)."""
    orch = _mock_orch(tmp_path, owner_email="owner@x.com")
    _bind_master_repo(orch.backend, "repo-a")
    orch._mint_friend_sheet = lambda r, repos, a: ("FID", "http://u")

    sid, url, repos, autonomy = orch.share_repos("", ["repo-a"])

    assert (sid, url, repos, autonomy) == ("FID", "http://u", ["repo-a"], "gated")
    friends = orch.backend.read_friends()
    assert len(friends) == 1
    assert friends[0].sheet_id == "FID"
    assert friends[0].recipient == ""          # blank — owner-distributed
    assert friends[0].repos == ["repo-a"]


def test_share_repos_blank_recipient_no_owner_rejected(tmp_path: Path):
    """Blank recipient AND no OWNER_EMAIL → reject, mint nothing (a file nobody but
    the service account could reach is never created)."""
    orch = _mock_orch(tmp_path, owner_email="")   # no owner to fall back to
    _bind_master_repo(orch.backend, "repo-a")
    minted: list[int] = []
    orch._mint_friend_sheet = lambda *a, **k: minted.append(1) or ("F", "u")

    with pytest.raises(ValueError):
        orch.share_repos("", ["repo-a"])

    assert minted == []
    assert orch.backend.read_friends() == []


def _stub_drive_seam(orch: Orchestrator, tmp_path: Path):
    """Make the REAL `_mint_friend_sheet` runnable offline: record create/share calls
    and replace the per-friend seeding GoogleBackend with a throw-away MockBackend."""
    titles: list[str] = []
    shares: list[tuple[str, str, str]] = []
    orch.backend.create_spreadsheet = lambda title: (titles.append(title)
                                                     or ("FID", "http://u/FID"))
    orch.backend.share_spreadsheet = (
        lambda sid, email, role="writer": shares.append((sid, email, role)))
    orch_mod.sheets.GoogleBackend = (   # seed-into-new-file backend, never real Google
        lambda sheet_id, sa_json: MockBackend(str(tmp_path / f"friend-{sheet_id}.json")))
    return titles, shares


def test_mint_friend_sheet_skips_recipient_share_when_blank(tmp_path: Path, monkeypatch):
    orch = _mock_orch(tmp_path, owner_email="owner@x.com")
    saved_gb = orch_mod.sheets.GoogleBackend
    titles, shares = _stub_drive_seam(orch, tmp_path)
    try:
        sid, url = orch._mint_friend_sheet("", ["repo-a", "repo-b"], "gated")
    finally:
        orch_mod.sheets.GoogleBackend = saved_gb

    assert (sid, url) == ("FID", "http://u/FID")
    # Only the owner-share ran — no recipient-share for a blank recipient.
    assert shares == [("FID", "owner@x.com", "writer")]
    # Title derived from the repo allowlist, never an empty recipient.
    assert titles == ["beelink • repo-a (2 repo)"]


def test_mint_friend_sheet_shares_owner_and_recipient_when_named(tmp_path: Path):
    orch = _mock_orch(tmp_path, owner_email="owner@x.com")
    saved_gb = orch_mod.sheets.GoogleBackend
    titles, shares = _stub_drive_seam(orch, tmp_path)
    try:
        orch._mint_friend_sheet("p@x.com", ["repo-a"], "gated")
    finally:
        orch_mod.sheets.GoogleBackend = saved_gb

    # Owner first, then the named recipient.
    assert shares == [("FID", "owner@x.com", "writer"), ("FID", "p@x.com", "writer")]
    assert titles == ["beelink • p@x.com (1 repo)"]


def test_share_cli_no_recipient_owner_distributed(tmp_path: Path, monkeypatch, capsys):
    from sheet_agent.__main__ import _share

    path = str(tmp_path / "m.json")
    _bind_master_repo(MockBackend(path), "repo-a")
    monkeypatch.setattr(
        Orchestrator, "_mint_friend_sheet",
        lambda self, r, repos, a: ("FID-CLI", "https://docs.google.com/d/FID-CLI"))

    cfg = C.Config(backend="mock", mock_path=path, owner_email="owner@x.com")
    rc = _share(cfg, "", "repo-a", None)       # no --recipient

    assert rc == 0
    assert "FID-CLI" in capsys.readouterr().out
    friends = MockBackend(path).read_friends()
    assert len(friends) == 1
    assert friends[0].recipient == ""          # owner-distributed
    assert friends[0].repos == ["repo-a"]


def test_share_cli_rejects_unbound_repo(tmp_path: Path, monkeypatch):
    from sheet_agent.__main__ import _share

    path = str(tmp_path / "m.json")
    MockBackend(path)  # empty master — nothing bound
    minted: list[int] = []
    monkeypatch.setattr(Orchestrator, "_mint_friend_sheet",
                        lambda self, *a: minted.append(1) or ("F", "u"))

    cfg = C.Config(backend="mock", mock_path=path)
    rc = _share(cfg, "p@x.com", "repo-a", None)

    assert rc == 2          # non-zero on a validation error
    assert minted == []     # nothing minted
    assert MockBackend(path).read_friends() == []
