"""Fail-loud tombstone registry for removed environment & config inputs (Finding 5 / 36c7).

A retired-but-still-set env var or TOML key that affects store location, write/sync
gates, auth, security, or lifecycle policy must fail LOUD — a targeted migration error
(old name + replacement + removed-in) and a non-zero exit — instead of being silently
ignored or defaulted. Operationally-inert retired inputs WARN (exit 0, process
continues). Genuinely-unknown keys keep the project's forward-compat policy (WARN, or
error only under REBAR_CONFIG_UNKNOWN_KEYS=error).

Error-class inputs raise ``RemovedInputError`` (a ``BaseException`` subclass, so a
``try/except ConfigError`` cannot swallow it into a silent fallback).

Tests assert OBSERVABLE behaviour only: CLI exit codes, stderr text, log records, the
raised exception type, and the absence of store mutation — never internals.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

import rebar


# ── helpers ───────────────────────────────────────────────────────────────────
def _cli(*args: str, cwd: str, **env: str) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e.update(env)
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=e,
    )


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _event_files(repo: Path) -> set[str]:
    return {p.name for p in _tracker(repo).rglob("*.json") if not p.name.startswith(".")}


def _dep():
    from rebar import _deprecations

    return _deprecations


# The seeded removed inputs (from the Finding-5 audit), by class.
ERROR_ENV = ["TICKETS_TRACKER_DIR", "REBAR_MCP_ALLOW_RECONCILE_LIVE"]
WARN_ENV = ["REBAR_PUSH", "REBAR_RECONCILER_LOCK_MAX_RETRIES"]
ERROR_CFG = ["verify.require_verdict_for_close"]


# ══════════════════════════════════════════════════════════════════════════════
#  HAPPY PATH (implementer sees this subset)
# ══════════════════════════════════════════════════════════════════════════════
def test_tombstone_vocabularies_and_types_defined() -> None:
    dep = _dep()
    assert dep._TOMBSTONE_KINDS == frozenset({"env", "cfg", "file"})
    assert dep._TOMBSTONE_BEHAVIORS == frozenset({"error", "warn"})
    # A distinct, un-swallowable error type — a BaseException subclass (NOT ConfigError).
    assert issubclass(dep.RemovedInputError, BaseException)
    assert not issubclass(dep.RemovedInputError, Exception)
    # RemovedInput record carries the migration fields.
    ri = dep.RemovedInput(
        kind="env", name="X_OLD", replacement="X_NEW", removed_in="v1.0", behavior="error"
    )
    assert (ri.kind, ri.name, ri.replacement, ri.removed_in, ri.behavior) == (
        "env",
        "X_OLD",
        "X_NEW",
        "v1.0",
        "error",
    )


def test_registry_seeded_with_known_retired_inputs() -> None:
    dep = _dep()
    names = {ri.name: ri for ri in dep.tombstones()}
    # Error-class store/security/policy/sync inputs.
    assert names["TICKETS_TRACKER_DIR"].behavior == "error"
    assert names["TICKETS_TRACKER_DIR"].replacement == "REBAR_TRACKER_DIR"
    assert names["REBAR_MCP_ALLOW_RECONCILE_LIVE"].behavior == "error"
    assert names["verify.require_verdict_for_close"].behavior == "error"
    # Operationally-inert warn-class inputs.
    assert names["REBAR_PUSH"].behavior == "warn"


def test_scan_tombstones_clean_env_returns_empty() -> None:
    dep = _dep()
    # A scan with no retired inputs present returns no matches and never raises.
    hits = dep.scan_tombstones(env={}, toml_tables={}, file_paths=[])
    assert hits == []


# ══════════════════════════════════════════════════════════════════════════════
#  HELD-OUT ORACLE (withheld from implementer)
# ══════════════════════════════════════════════════════════════════════════════


# ── error-class env var → fail loud, no store mutation ────────────────────────
@pytest.mark.parametrize("var", ERROR_ENV)
def test_error_env_var_fails_loud(rebar_repo: Path, var: str) -> None:
    dep = _dep()
    ri = {r.name: r for r in dep.tombstones()}[var]
    before = _event_files(rebar_repo)
    # A read/list command that resolves config must fail loud when the retired var is set.
    cp = _cli("list", cwd=str(rebar_repo), **{var: "some-value"})
    assert cp.returncode != 0, f"{var} did not fail loud: {cp.stdout}{cp.stderr}"
    out = cp.stdout + cp.stderr
    assert var in out, f"error must name the old input {var}"
    assert ri.replacement in out, f"error must name the replacement {ri.replacement}"
    assert ri.removed_in in out, f"error must name the removed-in version {ri.removed_in}"
    assert "Traceback" not in out, "must be a clean message, not a raw traceback"
    # No store mutation occurred.
    assert _event_files(rebar_repo) == before


# ── error-class TOML key → fail loud ──────────────────────────────────────────
def test_error_toml_key_fails_loud(rebar_repo: Path) -> None:
    # Standalone rebar.toml uses TOP-LEVEL sections (`[verify]`), not the pyproject-style
    # `[tool.rebar.verify]` nesting.
    (rebar_repo / "rebar.toml").write_text("[verify]\nrequire_verdict_for_close = true\n")
    cp = _cli("list", cwd=str(rebar_repo))
    assert cp.returncode != 0, f"retired TOML key did not fail loud: {cp.stdout}{cp.stderr}"
    out = cp.stdout + cp.stderr
    assert "require_verdict_for_close" in out
    assert "require_completion_verification_for_close" in out  # the replacement


# ── warn-class retired input → warn, exit 0, continue ─────────────────────────
def test_warn_env_var_warns_not_errors(rebar_repo: Path, monkeypatch, caplog) -> None:
    """A warn-class retired input logs a deprecation warning (old->replacement) on the
    `rebar` logger and lets the process continue (no raise, exit 0) — captured in-process
    via caplog since subprocess stderr logging is not reliably configured."""
    from rebar import config as cfg

    monkeypatch.setenv("REBAR_ROOT", str(rebar_repo))
    monkeypatch.setenv("REBAR_PUSH", "always")
    cfg.reset_config_cache()
    with caplog.at_level(logging.WARNING, logger="rebar"):
        cfg.load_config(str(rebar_repo))  # must NOT raise
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "REBAR_PUSH" in msgs and "REBAR_SYNC_PUSH" in msgs, (
        f"warn-class input must log old->replacement: {msgs!r}"
    )


# ── RemovedInputError is un-swallowable by `except ConfigError` ───────────────
def test_tracker_dir_override_raises_not_silently_ignored(monkeypatch) -> None:
    """Rewrite of the former silent-ignore contract: TICKETS_TRACKER_DIR now fails loud
    at the single env-read source, so a `except ConfigError` fallback cannot swallow it
    into a `.tickets-tracker` default (RemovedInputError is a BaseException)."""
    from rebar import config as cfg

    dep = _dep()
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.setenv("TICKETS_TRACKER_DIR", "/tmp/legacy")
    cfg.reset_config_cache()
    with pytest.raises(dep.RemovedInputError):
        cfg.tracker_dir_override()


# ── warm cache still fires (env check precedes the cache lookup) ──────────────
def test_warm_cache_still_fires(rebar_repo: Path, monkeypatch) -> None:
    from rebar import config as cfg

    dep = _dep()
    monkeypatch.setenv("REBAR_ROOT", str(rebar_repo))
    cfg.reset_config_cache()
    cfg.load_config(str(rebar_repo))  # prime the cache cleanly
    monkeypatch.setenv("REBAR_MCP_ALLOW_RECONCILE_LIVE", "1")
    with pytest.raises(dep.RemovedInputError):
        cfg.load_config(str(rebar_repo))


# ── MCP write path fails hard too (not only CLI) ─────────────────────────────
def test_mcp_write_fails_hard(rebar_repo: Path, monkeypatch) -> None:
    """A retired error-class input must refuse an MCP write, not downgrade it to a
    per-request error — RemovedInputError (BaseException) propagates out of the handler."""
    from rebar import config as cfg

    dep = _dep()
    monkeypatch.setenv("REBAR_ROOT", str(rebar_repo))
    cfg.reset_config_cache()
    cfg.load_config(str(rebar_repo))  # warm cache clean
    monkeypatch.setenv("TICKETS_TRACKER_DIR", "/tmp/legacy")
    with pytest.raises(dep.RemovedInputError):
        rebar.create_ticket(
            "task",
            "should not be created",
            description="Body.\n\n## Acceptance Criteria\n- [ ] a",
            repo_root=str(rebar_repo),
        )


# ── REBAR_LLM_MAX_ITERS tombstone in the llm layer ───────────────────────────
def test_llm_max_iters_tombstone(monkeypatch) -> None:
    dep = _dep()
    from rebar.llm import config as llm_config

    monkeypatch.setenv("REBAR_LLM_MAX_ITERS", "9")
    with pytest.raises(dep.RemovedInputError) as ei:
        llm_config.LLMConfig.from_env()
    assert "REBAR_LLM_MAX_STEPS" in str(ei.value)


# ── config validate collects ALL matches without raising ─────────────────────
def test_config_validate_reports_and_exits_nonzero(rebar_repo: Path) -> None:
    cp = _cli("config", "validate", cwd=str(rebar_repo), TICKETS_TRACKER_DIR="/tmp/legacy")
    assert cp.returncode != 0, "config validate must exit non-zero when an error-class input is set"
    out = cp.stdout + cp.stderr
    assert "TICKETS_TRACKER_DIR" in out and "REBAR_TRACKER_DIR" in out
    # Clean environment exits 0.
    clean = _cli("config", "validate", cwd=str(rebar_repo))
    assert clean.returncode == 0, f"clean env config validate must exit 0: {clean.stderr}"


# ── genuinely-unknown TOML key keeps forward-compat policy (WARN, not error) ──
def test_unknown_toml_key_keeps_forwardcompat_policy(rebar_repo: Path) -> None:
    (rebar_repo / "rebar.toml").write_text("[verify]\nsome_genuinely_unknown_future_key = true\n")
    # Default policy: WARN + ignore (exit 0). The tombstone registry covers only KNOWN
    # retired inputs; an unknown key must not be treated as a removed input.
    cp = _cli("list", cwd=str(rebar_repo))
    assert cp.returncode == 0, f"unknown key must not hard-fail by default: {cp.stderr}"


# ── docs are documented (deterministic proving check) ─────────────────────────
def test_docs_config_md_documents_tombstones() -> None:
    repo_root = Path(rebar.__file__).resolve().parents[2]
    text = (repo_root / "docs" / "config.md").read_text(encoding="utf-8")
    assert "tombstone" in text
    assert "rebar config validate" in text
