"""0ac6 (slice 2): route the reconciler.* tunables through the typed Config as the
config-FILE layer, so a `[tool.rebar.reconciler]` / `rebar.toml` value is actually
CONSUMED (not just shown by `rebar config`).

Covers the four consumers — acli_subprocess._acli_call_timeout (jira_cli_timeout),
_advisory_lock._resolve_retry_budget (lock_max_retries), outbound deletion-probe
budget (deletion_probe_limit), rebar_id_audit._resolve_id_guard_bypass
(id_guard_bypass_unsafe) — across config LOCATIONS (pyproject, rebar.toml, XDG user,
env, `rebar -c`) and asserts precedence CLI > env > project > user > default, the
ergonomic canonical env names (REBAR_JIRA_CLI_TIMEOUT, REBAR_UNSAFE_ID_GUARD_BYPASS)
+ EV-3c deprecated aliases, and the id-guard value-flip and fail-CLOSED default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar import config as cfg
from rebar_reconciler import acli_subprocess, rebar_id_audit

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REBAR_CONFIG",
        "XDG_CONFIG_HOME",
        "REBAR_ROOT",
        "REBAR_JIRA_CLI_TIMEOUT",
        "REBAR_ACLI_TIMEOUT",
        "REBAR_RECONCILER_LOCK_MAX_RETRIES",
        "REBAR_RECONCILER_LOCK_RETRY_BUDGET",
        "REBAR_RECONCILER_DELETION_PROBE_LIMIT",
        "RECONCILER_ABSENT_GET_BUDGET",
        "REBAR_UNSAFE_ID_GUARD_BYPASS",
        "REBAR_ID_GUARD_MODE",
    ):
        monkeypatch.delenv(name, raising=False)


def _proj(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    return p


def _xdg(tmp: Path, body: str) -> Path:
    """Write a user-level XDG config and return the XDG_CONFIG_HOME base."""
    base = tmp / "xdg"
    (base / "rebar").mkdir(parents=True)
    (base / "rebar" / "config.toml").write_text(body, encoding="utf-8")
    return base


# ── jira_cli_timeout — the canonical-env-name override + all file locations ────
def test_timeout_default_is_unset_zero_to_120(tmp_path: Path) -> None:
    # typed default is 0 (= "unset"); the consumer maps that to its 120s fallback.
    assert cfg.load_config(root=_proj(tmp_path)).reconciler.jira_cli_timeout == 0
    assert acli_subprocess._acli_call_timeout() == acli_subprocess._DEFAULT_ACLI_TIMEOUT


def test_timeout_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _proj(tmp_path)
    (p / "pyproject.toml").write_text(
        "[tool.rebar.reconciler]\njira_cli_timeout = 30\n", encoding="utf-8"
    )
    monkeypatch.setenv("REBAR_ROOT", str(p))
    assert acli_subprocess._acli_call_timeout() == 30


def test_timeout_rebar_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[reconciler]\njira_cli_timeout = 31\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(p))
    assert acli_subprocess._acli_call_timeout() == 31


def test_timeout_rebar_toml_alt_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[reconciler]\njira_cli_timeout = 32\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(p))
    assert acli_subprocess._acli_call_timeout() == 32


def test_timeout_canonical_env_is_the_nice_name(tmp_path: Path, monkeypatch) -> None:
    """The env override is REBAR_JIRA_CLI_TIMEOUT, NOT REBAR_RECONCILER_JIRA_CLI_TIMEOUT."""
    monkeypatch.setenv("REBAR_ROOT", str(_proj(tmp_path)))
    monkeypatch.setenv("REBAR_JIRA_CLI_TIMEOUT", "45")
    assert acli_subprocess._acli_call_timeout() == 45


def test_timeout_legacy_env_alias(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_ROOT", str(_proj(tmp_path)))
    monkeypatch.setenv("REBAR_ACLI_TIMEOUT", "33")
    assert acli_subprocess._acli_call_timeout() == 33


def test_timeout_precedence_cli_gt_env_gt_project_gt_user_gt_default(
    tmp_path: Path, monkeypatch
) -> None:
    p = _proj(tmp_path)
    user_base = _xdg(tmp_path, "[reconciler]\njira_cli_timeout = 10\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user_base))
    # user only (no project file) → 10
    assert cfg.load_config(root=p).reconciler.jira_cli_timeout == 10
    # project beats user → 20
    (p / "rebar.toml").write_text("[reconciler]\njira_cli_timeout = 20\n", encoding="utf-8")
    cfg.reset_config_cache()
    assert cfg.load_config(root=p).reconciler.jira_cli_timeout == 20
    # env beats project → 30
    monkeypatch.setenv("REBAR_ROOT", str(p))
    monkeypatch.setenv("REBAR_JIRA_CLI_TIMEOUT", "30")
    cfg.reset_config_cache()
    assert acli_subprocess._acli_call_timeout() == 30
    # cli beats env → 40
    cfg.set_cli_overrides(cfg.parse_cli_overrides(["reconciler.jira_cli_timeout=40"]))
    assert acli_subprocess._acli_call_timeout() == 40
    cfg.set_cli_overrides(None)


# (lock_max_retries + the b859 retry loop it tuned were removed in epic
#  dust-troth-naval / C4 — superseded by the self-healing ref lock. Its tests are
#  retired with it.)


# ── deletion_probe_limit (resolved value the outbound differ now reads) ────────
def test_deletion_probe_file_and_aliases(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "pyproject.toml").write_text(
        "[tool.rebar.reconciler]\ndeletion_probe_limit = 2\n", encoding="utf-8"
    )
    monkeypatch.setenv("REBAR_ROOT", str(p))
    assert cfg.load_config().reconciler.deletion_probe_limit == 2
    monkeypatch.setenv("RECONCILER_ABSENT_GET_BUDGET", "4")  # deprecated alias
    cfg.reset_config_cache()
    assert cfg.load_config().reconciler.deletion_probe_limit == 4
    monkeypatch.setenv("REBAR_RECONCILER_DELETION_PROBE_LIMIT", "9")  # canonical beats alias
    cfg.reset_config_cache()
    assert cfg.load_config().reconciler.deletion_probe_limit == 9


# ── id_guard_bypass_unsafe — value-flip, fail-closed, legacy flat key ─────────
def test_id_guard_default_active(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_ROOT", str(_proj(tmp_path)))
    assert rebar_id_audit._resolve_id_guard_bypass() is False  # fail-closed default


def test_id_guard_file_bypass(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[reconciler]\nid_guard_bypass_unsafe = true\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(p))
    assert rebar_id_audit._resolve_id_guard_bypass() is True


def test_id_guard_canonical_env_nice_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_ROOT", str(_proj(tmp_path)))
    monkeypatch.setenv("REBAR_UNSAFE_ID_GUARD_BYPASS", "true")
    assert rebar_id_audit._resolve_id_guard_bypass() is True


def test_id_guard_env_beats_file(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[reconciler]\nid_guard_bypass_unsafe = true\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(p))
    monkeypatch.setenv("REBAR_UNSAFE_ID_GUARD_BYPASS", "false")  # env keeps the guard ON
    assert rebar_id_audit._resolve_id_guard_bypass() is False


@pytest.mark.parametrize("mode,expect", [("warn", True), ("raise", False)])
def test_id_guard_legacy_env_value_flip(tmp_path: Path, monkeypatch, mode, expect) -> None:
    monkeypatch.setenv("REBAR_ROOT", str(_proj(tmp_path)))
    monkeypatch.setenv("REBAR_ID_GUARD_MODE", mode)
    assert rebar_id_audit._resolve_id_guard_bypass() is expect


def test_id_guard_fail_closed_on_malformed_config(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(
        "[reconciler]\nid_guard_bypass_unsafe = not_a_bool\n", encoding="utf-8"
    )
    monkeypatch.setenv("REBAR_ROOT", str(p))
    # An invalid value raises ConfigError inside load_config → guard FAILS CLOSED.
    assert rebar_id_audit._resolve_id_guard_bypass() is False
    # The tunables fall back to their safe defaults rather than failing the pass.
    assert acli_subprocess._acli_call_timeout() == acli_subprocess._DEFAULT_ACLI_TIMEOUT


# ── `rebar config` provenance: reported layer matches the consumed value ───────
def test_show_config_provenance(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[reconciler]\njira_cli_timeout = 20\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(p))
    monkeypatch.setenv("REBAR_JIRA_CLI_TIMEOUT", "30")
    config, sources, _ = cfg.resolve_with_sources(root=p)
    assert config.reconciler.jira_cli_timeout == 30
    assert sources["reconciler"]["jira_cli_timeout"] == "env"  # env beat the project file


# ── baseline_consumer_swap (story a118) — default + rebar.toml round-trip ──────
def test_baseline_consumer_swap_default_false(tmp_path: Path) -> None:
    cfg.reset_config_cache()
    assert cfg.load_config(root=_proj(tmp_path)).reconciler.baseline_consumer_swap is False


def test_baseline_consumer_swap_rebar_toml_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the `_SECTIONS["reconciler"]` coercer entry exists — without it the TOML
    key is silently dropped and the value stays False."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[reconciler]\nbaseline_consumer_swap = true\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(p))
    cfg.reset_config_cache()
    assert cfg.load_config(root=p).reconciler.baseline_consumer_swap is True
    cfg.reset_config_cache()
