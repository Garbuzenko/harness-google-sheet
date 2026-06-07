"""Offline tests for the state/runs/ forensic-dump rotation. Unbounded, these dumps
fill the host disk (108 MB seen live); _prune_runs caps them to the newest N."""
from __future__ import annotations

from pathlib import Path

from sheet_agent.agent import _persist_raw, _prune_runs


def _touch(runs: Path, name: str, mtime: float) -> Path:
    p = runs / name
    p.write_text("{}")
    import os
    os.utime(p, (mtime, mtime))
    return p


def test_prune_keeps_newest_and_drops_oldest(tmp_path: Path):
    runs = tmp_path
    for i in range(10):
        _touch(runs, f"r-{i}.json", mtime=1000.0 + i)   # i=9 newest
    _prune_runs(runs, keep=3)
    survivors = sorted(p.name for p in runs.glob("*.json"))
    assert survivors == ["r-7.json", "r-8.json", "r-9.json"]


def test_prune_noop_when_under_cap(tmp_path: Path):
    runs = tmp_path
    for i in range(3):
        _touch(runs, f"r-{i}.json", mtime=1000.0 + i)
    _prune_runs(runs, keep=5)
    assert len(list(runs.glob("*.json"))) == 3


def test_prune_disabled_with_zero_keeps_everything(tmp_path: Path):
    runs = tmp_path
    for i in range(8):
        _touch(runs, f"r-{i}.json", mtime=1000.0 + i)
    _prune_runs(runs, keep=0)
    assert len(list(runs.glob("*.json"))) == 8


def test_prune_only_touches_json(tmp_path: Path):
    runs = tmp_path
    (runs / "keep.txt").write_text("not a dump")
    for i in range(5):
        _touch(runs, f"r-{i}.json", mtime=1000.0 + i)
    _prune_runs(runs, keep=1)
    assert (runs / "keep.txt").exists()
    assert len(list(runs.glob("*.json"))) == 1


def test_prune_survives_missing_dir(tmp_path: Path):
    # never raises even if the directory does not exist
    _prune_runs(tmp_path / "nonexistent", keep=2)


def test_persist_raw_writes_then_prunes(tmp_path: Path):
    runs = tmp_path
    for i in range(4):
        _persist_raw(runs, "repo", f'{{"i": {i}}}', keep=2)
    # each call writes one dump then trims to the newest 2
    assert len(list(runs.glob("*.json"))) == 2


def test_persist_raw_default_keep_is_unbounded(tmp_path: Path):
    runs = tmp_path
    for i in range(5):
        _persist_raw(runs, "repo", "{}")            # keep defaults to 0 → no prune
    assert len(list(runs.glob("*.json"))) == 5
