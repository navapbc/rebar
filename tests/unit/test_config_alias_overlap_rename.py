"""Story 9416: `verify.overlap_enabled` -> `verify.suggest_duplicate_tickets`.

The rename names what the setting PRODUCES (advisory duplicate-link suggestions)
rather than the internal mechanism it toggles. Both the old TOML key and the old
env var stay honored FOREVER as permanent aliases, so an untouched project config
keeps working. These tests pin the two alias paths, the canonical-wins precedence
both of them branch on, the unchanged default, and the permanent (not scheduled)
classification of the two registry rows.
"""

from __future__ import annotations

import logging
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


# ── TOML alias ────────────────────────────────────────────────────────────────
def test_toml_alias_resolves(caplog: pytest.LogCaptureFixture) -> None:
    """An untouched project config using the old key still turns the feature on."""
    with caplog.at_level(logging.WARNING, logger="rebar._config_schema"):
        out = coerce_sparse({"verify": {"overlap_enabled": "true"}})
    assert out == {"verify": {"suggest_duplicate_tickets": True}}
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "permanent alias" in joined
    assert "scheduled for removal" not in joined


# ── env alias ─────────────────────────────────────────────────────────────────
def test_env_alias_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old env var still turns the feature on when the canonical var is unset."""
    monkeypatch.setenv(_LEGACY_ENV, "1")
    assert cfg.env_overrides().get("verify") == {"suggest_duplicate_tickets": "1"}


def test_canonical_env_name_is_derived_from_the_new_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_CANONICAL_ENV, "1")
    assert cfg.env_overrides().get("verify") == {"suggest_duplicate_tickets": "1"}


# ── precedence: canonical wins ────────────────────────────────────────────────
def test_canonical_wins_over_legacy(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """With BOTH set — in TOML or in the environment — the canonical value wins."""
    # TOML: canonical wins and the legacy key is dropped WITHOUT a warning.
    with caplog.at_level(logging.WARNING, logger="rebar._config_schema"):
        out = coerce_sparse(
            {"verify": {"overlap_enabled": "false", "suggest_duplicate_tickets": "true"}}
        )
    assert out == {"verify": {"suggest_duplicate_tickets": True}}
    assert not [r for r in caplog.records if "overlap_enabled" in r.getMessage()]

    # env: the canonical var wins, so the legacy truthy value does not turn it on.
    monkeypatch.setenv(_LEGACY_ENV, "1")
    monkeypatch.setenv(_CANONICAL_ENV, "0")
    assert cfg.env_overrides().get("verify") == {"suggest_duplicate_tickets": "0"}


# ── the registry rows ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("cfg:verify.overlap_enabled", "verify.suggest_duplicate_tickets"),
        ("env:REBAR_VERIFY_OVERLAP_ENABLED", _CANONICAL_ENV),
    ],
)
def test_rows_are_permanent(key: str, replacement: str) -> None:
    """Both alias rows are PERMANENT renames — not scheduled supersessions."""
    row = dep.REGISTRY[key]
    assert row.permanent is True
    assert row.remove_in is None
    assert row.replacement == replacement
    msg = dep.warn_deprecated(key)
    assert "permanent alias" in msg
    assert "scheduled for removal" not in msg


def test_legacy_env_alias_resolution_table_carries_the_rename() -> None:
    assert cfg._LEGACY_ENV_ALIASES[_LEGACY_ENV] == (
        "verify",
        "suggest_duplicate_tickets",
        _CANONICAL_ENV,
    )
