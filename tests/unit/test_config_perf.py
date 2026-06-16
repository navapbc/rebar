"""Performance validation for config resolution (config-refinement task e211).

Config resolution is on the COMMAND hot path (every CLI invocation + the verify
gate + ticket.display_mode go through ``load_config``). These tests prove the
resolution is parsed/discovered ONCE and cached per process — no per-call re-parse
or repeated upward directory walk — without regressing correctness, and that the
fail-closed verify path is never cached. They are call-COUNT based (robust, not
wall-clock-flaky), plus one generous warm-resolution budget as a smoke guard.
"""

from __future__ import annotations

import os
import time
import tomllib
from pathlib import Path

import pytest

from rebar import config as cfg

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REBAR_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for sect, keys in cfg._SECTIONS.items():
        for key in keys:
            monkeypatch.delenv(f"REBAR_{sect.upper()}_{key.upper()}", raising=False)


def _proj(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir()
    (p / ".git").mkdir()
    return p


def _count_parses(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every real tomllib parse (cache MISS). Returns the growing list."""
    parsed: list[Path] = []
    real = tomllib.load

    def _spy(fp):  # fp is the open binary file handle
        parsed.append(Path(fp.name))
        return real(fp)

    monkeypatch.setattr(tomllib, "load", _spy)
    return parsed


def _count_walks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = cfg._discover_project_config

    def _spy(root=None):
        calls.append(1)
        return real(root)

    monkeypatch.setattr(cfg, "_discover_project_config", _spy)
    return calls


# ── caching: parsed/discovered ONCE per process ───────────────────────────────
def test_warm_load_skips_discovery_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'async'\n", encoding="utf-8")
    walks = _count_walks(monkeypatch)
    a = cfg.load_config(root=p)
    b = cfg.load_config(root=p)  # identical key → served from cache
    assert a is b  # same object, not just equal
    assert len(walks) == 1  # discovery walk ran exactly once


def test_no_double_parse_of_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pyproject with [tool.rebar] is the discovered config: the discovery
    presence-probe and the table read must share ONE parse, not two."""
    p = _proj(tmp_path)
    (p / "pyproject.toml").write_text(
        "[project]\nname='x'\n[tool.rebar.sync]\npush = 'off'\n", encoding="utf-8"
    )
    parsed = _count_parses(monkeypatch)
    cfg.load_config(root=p)
    pyproject_parses = [q for q in parsed if q.name == "pyproject.toml"]
    assert len(pyproject_parses) == 1  # parsed once, not twice


def test_toml_cache_dedups_repeated_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _proj(tmp_path) / "rebar.toml"
    rt.write_text("[compact]\nthreshold = 9\n", encoding="utf-8")
    parsed = _count_parses(monkeypatch)
    cfg._read_toml_table(rt, pyproject=False)
    cfg._read_toml_table(rt, pyproject=False)
    assert len([q for q in parsed if q.name == "rebar.toml"]) == 1  # mtime-cache hit


def test_reset_forces_rewalk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'async'\n", encoding="utf-8")
    walks = _count_walks(monkeypatch)
    cfg.load_config(root=p)
    cfg.reset_config_cache()
    cfg.load_config(root=p)
    assert len(walks) == 2  # cache cleared → re-resolved


def test_distinct_env_keys_are_separate_cache_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _proj(tmp_path)
    a = cfg.load_config(root=p)
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    b = cfg.load_config(root=p)  # env changed → different key → fresh resolve
    assert a.sync.push == "always" and b.sync.push == "off"


# ── freshness: an in-process config EDIT is honored (no stale cache) ──────────
def test_in_process_edit_is_picked_up(tmp_path: Path) -> None:
    """The load-bearing safety property: in a long-running process, EDITING the
    verify gate in the config must take effect on the next resolve — the result
    cache re-stats the files it read and re-resolves on change (NOT a stale hit)."""
    p = _proj(tmp_path)
    pp = p / "pyproject.toml"
    pp.write_text(
        "[tool.rebar.verify]\nrequire_signature_for_close = false\n", encoding="utf-8"
    )
    assert cfg.load_config(root=p).verify.require_signature_for_close is False
    # Operator tightens the gate (same process, same cache key); bump mtime to be
    # filesystem-resolution-independent.
    pp.write_text(
        "[tool.rebar.verify]\nrequire_signature_for_close = true\n", encoding="utf-8"
    )
    os.utime(pp, (pp.stat().st_atime + 5, pp.stat().st_mtime + 5))
    assert cfg.load_config(root=p).verify.require_signature_for_close is True  # honored


def test_implicit_root_keys_on_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With root=None the repo root comes from cwd; two repos with different config
    must not bleed across a chdir (cwd is part of the cache key)."""
    monkeypatch.delenv("REBAR_ROOT", raising=False)
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    for name in ("A", "B"):
        r = tmp_path / name
        r.mkdir()
        (r / ".git").mkdir()
    (tmp_path / "A" / "rebar.toml").write_text("[compact]\nthreshold = 11\n", encoding="utf-8")
    (tmp_path / "B" / "rebar.toml").write_text("[compact]\nthreshold = 22\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path / "A")
    assert cfg.load_config().compact.threshold == 11
    monkeypatch.chdir(tmp_path / "B")
    assert cfg.load_config().compact.threshold == 22  # not the cached A value


# ── fail-closed verify gate is never cached as success ────────────────────────
def test_config_error_not_cached(tmp_path: Path) -> None:
    """A malformed (would-be) config raises ConfigError on EVERY call — an error is
    never cached, so the verify gate re-evaluates fail-closed each time."""
    p = _proj(tmp_path)
    (p / "pyproject.toml").write_text("[tool.rebar] broken === [[\n", encoding="utf-8")
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(root=p)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(root=p)  # still raises (success was not cached)


# ── bounded walk + generous warm budget (smoke) ───────────────────────────────
def test_deep_tree_walk_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery stops at the .git boundary — a deep subtree does NOT stat its way
    to the filesystem root (no unbounded stat storm)."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[compact]\nthreshold = 3\n", encoding="utf-8")
    deep = p
    for i in range(40):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    parsed = _count_parses(monkeypatch)
    c = cfg.load_config(root=deep)
    assert c.compact.threshold == 3  # found by walking up to the project root
    # Only the project's rebar.toml is parsed; the walk stops at .git, so no parse
    # of anything outside the repo.
    assert all(str(p) in str(q) for q in parsed)


def test_warm_resolution_is_cheap(tmp_path: Path) -> None:
    """Smoke budget: 1000 warm resolutions complete well under a generous ceiling
    (cache hits are dictionary lookups, not filesystem walks)."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'async'\n", encoding="utf-8")
    cfg.load_config(root=p)  # warm the cache
    start = time.perf_counter()
    for _ in range(1000):
        cfg.load_config(root=p)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5  # 1000 cached resolves in <0.5s (typically <10ms)
