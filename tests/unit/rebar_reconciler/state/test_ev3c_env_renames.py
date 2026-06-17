"""EV-3c: reconciler/LLM tunable renames + id-guard value-flip, each with an
old-name deprecation alias. The reconciler engine is on sys.path via the package
conftest, so the modules import flat.
"""

from __future__ import annotations

import pytest

from rebar_reconciler import _advisory_lock, acli_subprocess, rebar_id_audit

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
        "REBAR_LLM_MAX_STEPS",
        "REBAR_LLM_MAX_ITERS",
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


# ── LOCK_RETRY_BUDGET -> LOCK_MAX_RETRIES (fixes the dup-name no-op alias) ─────
def test_lock_retries_canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_RECONCILER_LOCK_MAX_RETRIES", "7")
    assert _advisory_lock._resolve_retry_budget() == 7


def test_lock_retries_legacy_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_RECONCILER_LOCK_RETRY_BUDGET", "3")
    assert _advisory_lock._resolve_retry_budget() == 3


def test_lock_retries_canonical_beats_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_RECONCILER_LOCK_MAX_RETRIES", "7")
    monkeypatch.setenv("REBAR_RECONCILER_LOCK_RETRY_BUDGET", "3")
    assert _advisory_lock._resolve_retry_budget() == 7


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


def test_id_guard_legacy_config_value_flip(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The legacy .rebar/config.conf key rebar_id_guard_mode=warn -> bypass True (no
    env set), exercising the config-file branch of the value-flip."""
    (tmp_path / ".rebar").mkdir()
    (tmp_path / ".rebar" / "config.conf").write_text("rebar_id_guard_mode=warn\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    assert rebar_id_audit._resolve_id_guard_bypass() is True
    # raise (or absent) -> guard active
    (tmp_path / ".rebar" / "config.conf").write_text(
        "rebar_id_guard_mode=raise\n", encoding="utf-8"
    )
    assert rebar_id_audit._resolve_id_guard_bypass() is False


# ── REBAR_LLM_MAX_ITERS -> REBAR_LLM_MAX_STEPS ────────────────────────────────
def test_llm_max_steps_canonical_and_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar.llm.config import _env_int_aliased

    monkeypatch.setenv("REBAR_LLM_MAX_STEPS", "40")
    assert _env_int_aliased("REBAR_LLM_MAX_STEPS", "REBAR_LLM_MAX_ITERS", 25) == 40
    monkeypatch.delenv("REBAR_LLM_MAX_STEPS")
    monkeypatch.setenv("REBAR_LLM_MAX_ITERS", "20")  # deprecated alias
    assert _env_int_aliased("REBAR_LLM_MAX_STEPS", "REBAR_LLM_MAX_ITERS", 25) == 20
