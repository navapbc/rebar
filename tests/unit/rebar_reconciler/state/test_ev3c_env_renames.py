"""EV-3c: reconciler/LLM tunable renames + id-guard value-flip, each with an
old-name deprecation alias. The reconciler engine is on sys.path via the package
conftest, so the modules import flat.
"""

from __future__ import annotations

import pytest

from rebar_reconciler import acli_subprocess, rebar_id_audit

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REBAR_JIRA_CLI_TIMEOUT",
        "REBAR_ACLI_TIMEOUT",
        "REBAR_RECONCILER_LOCK_MAX_RETRIES",
        "REBAR_RECONCILER_LOCK_RETRY_BUDGET",
        "REBAR_UNSAFE_ID_GUARD_BYPASS",
        "REBAR_ID_GUARD_MODE",
        "REBAR_ROOT",
        "REBAR_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)


# ── REBAR_ACLI_TIMEOUT -> REBAR_JIRA_CLI_TIMEOUT ──────────────────────────────
def test_acli_timeout_canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_JIRA_CLI_TIMEOUT", "45")
    assert acli_subprocess._acli_call_timeout() == 45


def test_acli_timeout_legacy_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_ACLI_TIMEOUT", "33")
    assert acli_subprocess._acli_call_timeout() == 33


def test_acli_timeout_canonical_beats_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_JIRA_CLI_TIMEOUT", "45")
    monkeypatch.setenv("REBAR_ACLI_TIMEOUT", "33")
    assert acli_subprocess._acli_call_timeout() == 45


# (LOCK_RETRY_BUDGET / LOCK_MAX_RETRIES env aliases + the lock_max_retries key were
#  removed in epic dust-troth-naval / C4 — the b859 retry loop they tuned is
#  superseded by the self-healing ref lock. Their tests are retired with them.)


# ── REBAR_ID_GUARD_MODE -> REBAR_UNSAFE_ID_GUARD_BYPASS (value-flip) ───────────
def test_id_guard_default_active(monkeypatch: pytest.MonkeyPatch) -> None:
    assert rebar_id_audit._resolve_id_guard_bypass() is False  # guard active


@pytest.mark.parametrize(
    "val,expect", [("true", True), ("1", True), ("false", False), ("0", False)]
)
def test_id_guard_canonical_bypass(monkeypatch: pytest.MonkeyPatch, val: str, expect: bool) -> None:
    monkeypatch.setenv("REBAR_UNSAFE_ID_GUARD_BYPASS", val)
    assert rebar_id_audit._resolve_id_guard_bypass() is expect


@pytest.mark.parametrize("mode,expect", [("warn", True), ("raise", False)])
def test_id_guard_legacy_env_value_flip(
    monkeypatch: pytest.MonkeyPatch, mode: str, expect: bool
) -> None:
    """Deprecated REBAR_ID_GUARD_MODE maps warn->bypass(True), raise->guard(False)."""
    monkeypatch.setenv("REBAR_ID_GUARD_MODE", mode)
    assert rebar_id_audit._resolve_id_guard_bypass() is expect


def test_id_guard_canonical_beats_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_UNSAFE_ID_GUARD_BYPASS", "false")  # guard active
    monkeypatch.setenv("REBAR_ID_GUARD_MODE", "warn")  # would bypass — but canonical wins
    assert rebar_id_audit._resolve_id_guard_bypass() is False


# (The REBAR_LLM_MAX_ITERS -> REBAR_LLM_MAX_STEPS alias was removed in ticket 5899;
#  only the canonical REBAR_LLM_MAX_STEPS is honored. Canonical resolution is covered
#  by tests/unit/test_config_llm.py::test_max_steps_legacy_env_alias_removed.)
