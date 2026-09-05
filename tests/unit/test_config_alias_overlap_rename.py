"""Story 9416 after ADR 0116: only verify.suggest_duplicate_tickets remains live.

The old TOML key and env var are retired by clean removal: they are no longer
aliases, while the canonical key/env retains the previous typed behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar import _deprecations as dep
from rebar import config as cfg
from rebar._config_schema import _SECTIONS, VerifyConfig, coerce_sparse

pytestmark = pytest.mark.unit

_LEGACY_ENV = "REBAR_VERIFY_OVERLAP_ENABLED"
_CANONICAL_ENV = "REBAR_VERIFY_SUGGEST_DUPLICATE_TICKETS"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("REBAR_CONFIG", "XDG_CONFIG_HOME", _LEGACY_ENV, _CANONICAL_ENV):
        monkeypatch.delenv(name, raising=False)


def _proj(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    return p


# ── the canonical key ─────────────────────────────────────────────────────────
def test_canonical_key_is_the_live_surface() -> None:
    """The new name is the field, the _SECTIONS coercer key, and defaults OFF."""
    assert VerifyConfig().suggest_duplicate_tickets is False
    assert "suggest_duplicate_tickets" in _SECTIONS["verify"]
    coerce = _SECTIONS["verify"]["suggest_duplicate_tickets"]
    assert coerce("true", "verify.suggest_duplicate_tickets") is True
    # the old name is no longer a live key
    assert "overlap_enabled" not in _SECTIONS["verify"]
    assert not hasattr(VerifyConfig(), "overlap_enabled")


def test_default_is_unchanged_when_nothing_is_set(tmp_path: Path) -> None:
    """Neither key nor env set -> the default did not move."""
    assert cfg.load_config(root=_proj(tmp_path)).verify.suggest_duplicate_tickets is False


# ── removed TOML alias ───────────────────────────────────────────────────────
def test_toml_alias_is_ignored_as_unknown(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="rebar.config"):
        out = coerce_sparse({"verify": {"overlap_enabled": "true"}})
    assert out == {}
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "verify.overlap_enabled" in joined
    assert "permanent alias" not in joined


# ── env alias ─────────────────────────────────────────────────────────────────
def test_env_alias_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_LEGACY_ENV, "1")
    assert cfg.env_overrides().get("verify") is None


def test_canonical_env_name_is_derived_from_the_new_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_CANONICAL_ENV, "1")
    assert cfg.env_overrides().get("verify") == {"suggest_duplicate_tickets": "1"}


# ── precedence: canonical wins ────────────────────────────────────────────────
def test_canonical_wins_over_legacy(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """With both set, the canonical value is the only value consumed."""
    with caplog.at_level("WARNING", logger="rebar.config"):
        out = coerce_sparse(
            {"verify": {"overlap_enabled": "false", "suggest_duplicate_tickets": "true"}}
        )
    assert out == {"verify": {"suggest_duplicate_tickets": True}}
    assert any("verify.overlap_enabled" in r.getMessage() for r in caplog.records)
    assert not any("permanent alias" in r.getMessage() for r in caplog.records)

    monkeypatch.setenv(_LEGACY_ENV, "1")
    monkeypatch.setenv(_CANONICAL_ENV, "0")
    assert cfg.env_overrides().get("verify") == {"suggest_duplicate_tickets": "0"}


# ── removed registry rows ───────────────────────────────────────────────────
def test_rows_are_removed_from_deprecation_registry() -> None:
    assert "cfg:verify.overlap_enabled" not in dep.REGISTRY
    assert f"env:{_LEGACY_ENV}" not in dep.REGISTRY


def test_legacy_env_alias_resolution_table_no_longer_carries_the_rename() -> None:
    assert _LEGACY_ENV not in cfg._LEGACY_ENV_ALIASES
