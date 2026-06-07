"""Offline tests for config.py — env coercion (_int/_bool) and the validate()
edge cases test_core.py does not cover (google-backend creds, poll/runs bounds)."""
from __future__ import annotations

from pathlib import Path

from sheet_agent import config as C
from sheet_agent.config import _bool, _int


def _problems(**kw) -> list[str]:
    return C.Config(**kw).validate()


def test_validate_google_backend_requires_sheet_and_sa(tmp_path: Path):
    probs = _problems(backend="google", sheet_id="", sa_json="")
    assert any("SHEET_ID" in p for p in probs)
    assert any("GOOGLE_SA_JSON" in p and "empty" in p for p in probs)


def test_validate_google_backend_flags_missing_sa_file(tmp_path: Path):
    missing = tmp_path / "nope.json"
    probs = _problems(backend="google", sheet_id="abc", sa_json=str(missing))
    assert any("not found" in p for p in probs)


def test_validate_mock_backend_needs_no_creds():
    assert _problems(backend="mock", autonomy="gated") == []


def test_validate_bad_friend_autonomy():
    assert any("FRIEND_DEFAULT_AUTONOMY" in p
               for p in _problems(backend="mock", friend_default_autonomy="bogus"))


def test_validate_poll_interval_and_runs_keep_bounds():
    assert any("POLL_INTERVAL" in p for p in _problems(backend="mock", poll_interval=0))
    assert any("RUNS_KEEP" in p for p in _problems(backend="mock", runs_keep=-1))
    # 0 is the legal "disable pruning" value, not an error
    assert not any("RUNS_KEEP" in p for p in _problems(backend="mock", runs_keep=0))


def test_int_parses_and_falls_back(monkeypatch):
    monkeypatch.setenv("POLL_INTERVAL", "45")
    assert _int("POLL_INTERVAL", 30) == 45
    # a typo'd value falls back to the default rather than crashing
    monkeypatch.setenv("POLL_INTERVAL", "30s")
    assert _int("POLL_INTERVAL", 30) == 30
    monkeypatch.delenv("POLL_INTERVAL", raising=False)
    assert _int("POLL_INTERVAL", 30) == 30


def test_bool_parsing(monkeypatch):
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv("AUTO_OPENSPEC_INIT", truthy)
        assert _bool("AUTO_OPENSPEC_INIT", False) is True
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("AUTO_OPENSPEC_INIT", falsy)
        assert _bool("AUTO_OPENSPEC_INIT", True) is False
    monkeypatch.delenv("AUTO_OPENSPEC_INIT", raising=False)
    assert _bool("AUTO_OPENSPEC_INIT", True) is True


def test_runs_keep_reads_env(monkeypatch):
    monkeypatch.setenv("RUNS_KEEP", "50")
    assert C.Config(backend="mock").runs_keep == 50
