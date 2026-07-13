"""Config unknown-key policy: unknown keys WARN during the deprecation window and
HARD-ERROR past the cutover (``REBAR_CONFIG_UNKNOWN_KEYS=error``), with cross-layer
precedence preserved.

(The legacy flat ``.rebar/config.conf`` reader and the ``verify.require_verdict_for_close``
alias were removed pre-1.0 — DE7 — so config now loads only from ``rebar.toml`` / a
``[tool.rebar]`` pyproject table, and the old alias is just an unknown key now.)
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from rebar import config as cfg

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REBAR_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("REBAR_CONFIG_UNKNOWN_KEYS", raising=False)
    for sect, keys in cfg._SECTIONS.items():
        for key in keys:
            monkeypatch.delenv(f"REBAR_{sect.upper()}_{key.upper()}", raising=False)


def _proj(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    return p


# ── removed alias: verify.require_verdict_for_close is now an unknown key ──────
def test_removed_verdict_alias_ignored_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The scheduled ``verify.require_verdict_for_close`` alias was removed pre-1.0
    (DE7): it no longer maps to the canonical key — it is just an unknown key now
    (warned + ignored), so the canonical stays at its default (False)."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[verify]\nrequire_verdict_for_close = true\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        c = cfg.load_config(root=p)
    assert c.verify.overlap_enabled is False  # canonical key still parses; not aliased
    assert any("require_verdict_for_close" in r.getMessage() for r in caplog.records)


# ── removed key: verify.require_signature_for_close is now an unknown key ──────
def test_removed_signature_close_key_ignored_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The ``verify.require_signature_for_close`` signature close-gate was retired
    (story 28f1) — this project's close gate is the completion verifier. Setting the
    old key is now just an unknown key (warned + ignored), and ``VerifyConfig`` no
    longer carries the attribute."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(
        "[verify]\nrequire_signature_for_close = true\n", encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        c = cfg.load_config(root=p)
    assert not hasattr(c.verify, "require_signature_for_close")
    assert any("require_signature_for_close" in r.getMessage() for r in caplog.records)


# ── unknown-key policy: WARN during window, ERROR past cutover ────────────────
def test_unknown_key_warns_during_window(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'off'\nbogus = 1\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        c = cfg.load_config(root=p)  # default policy = warn
    assert c.sync.push == "off"  # the good key still applies
    assert any("sync.bogus" in r.getMessage() for r in caplog.records)


def test_unknown_key_errors_under_strict_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REBAR_CONFIG_UNKNOWN_KEYS", "error")
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'off'\nbogus = 1\n", encoding="utf-8")
    with pytest.raises(cfg.ConfigError, match="sync.bogus"):
        cfg.load_config(root=p)


def test_unknown_section_errors_under_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REBAR_CONFIG_UNKNOWN_KEYS", "error")
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[nonsense]\nx = 1\n", encoding="utf-8")
    with pytest.raises(cfg.ConfigError, match=r"\[nonsense\]"):
        cfg.load_config(root=p)


def test_strict_loads_known_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Strict mode loads recognized keys without error."""
    monkeypatch.setenv("REBAR_CONFIG_UNKNOWN_KEYS", "error")
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(
        "[verify]\nrequire_plan_review_for_claim = true\n[sync]\npush = 'off'\n", encoding="utf-8"
    )
    c = cfg.load_config(root=p)
    assert c.verify.require_plan_review_for_claim is True and c.sync.push == "off"


def test_strict_errors_on_removed_verdict_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The removed ``require_verdict_for_close`` alias (DE7) is now a truly-unknown
    key, so under the strict cutover it hard-errors like any other unknown key."""
    monkeypatch.setenv("REBAR_CONFIG_UNKNOWN_KEYS", "error")
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[verify]\nrequire_verdict_for_close = true\n", encoding="utf-8")
    with pytest.raises(cfg.ConfigError, match="require_verdict_for_close"):
        cfg.load_config(root=p)


def test_strict_policy_unrecognized_value_falls_back_to_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REBAR_CONFIG_UNKNOWN_KEYS", "yelp")  # not 'error'
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'off'\nbogus = 1\n", encoding="utf-8")
    assert cfg.load_config(root=p).sync.push == "off"  # warns, does not raise


def test_invalid_value_still_fails_closed_regardless_of_policy(tmp_path: Path) -> None:
    """An invalid VALUE always raises (fail-closed at load) — independent of the
    unknown-key policy, which governs only UNKNOWN keys."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'sometimes'\n", encoding="utf-8")
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(root=p)


# ── from_mapping strict (direct API) ──────────────────────────────────────────
def test_from_mapping_strict_flag() -> None:
    assert cfg.Config.from_mapping({"sync": {"push": "off"}}, strict=True).sync.push == "off"
    with pytest.raises(cfg.ConfigError):
        cfg.Config.from_mapping({"sync": {"nope": 1}}, strict=True)
