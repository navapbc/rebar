"""Back-compat alias window for the legacy flat ``.rebar/config.conf`` (task 83e6):
the legacy file reads IDENTICALLY (parity with the typed TOML path), legacy keys
are aliased to their canonical names with a deprecation warning, unknown keys WARN
during the deprecation window and HARD-ERROR past the cutover
(``REBAR_CONFIG_UNKNOWN_KEYS=error``), with cross-layer precedence preserved.
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


def _legacy(p: Path, body: str) -> None:
    (p / ".rebar").mkdir(exist_ok=True)
    (p / ".rebar" / "config.conf").write_text(body, encoding="utf-8")


# ── parity: legacy flat conf reads identically to the typed TOML path ─────────
def test_legacy_conf_parity_with_toml(tmp_path: Path) -> None:
    """The same settings via the legacy flat conf and via rebar.toml resolve to an
    IDENTICAL typed Config — the legacy reader is a faithful front-end, not a
    second semantics."""
    pa = _proj(tmp_path / "a")
    _legacy(
        pa,
        "# leading comment\n"
        "verify.require_signature_for_close=true\n"
        'ticket.display_mode="plain"\n'  # quoted value tolerated
        "sync.pull = off\n"  # spaces around '=' tolerated
        "compact.threshold=42\n",
    )
    pb = _proj(tmp_path / "b")
    (pb / "rebar.toml").write_text(
        "[verify]\nrequire_signature_for_close = true\n"
        "[ticket]\ndisplay_mode = 'plain'\n"
        "[sync]\npull = 'off'\n"
        "[compact]\nthreshold = 42\n",
        encoding="utf-8",
    )
    ca = cfg.load_config(root=pa)
    cb = cfg.load_config(root=pb)
    assert ca == cb
    assert ca.verify.require_signature_for_close is True
    assert ca.ticket.display_mode == "plain" and ca.sync.pull == "off"
    assert ca.compact.threshold == 42


def test_legacy_conf_skips_blank_comment_and_keyless_lines(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    _legacy(p, "\n# a comment\n   \nthis-line-has-no-equals\nsync.push=async\n")
    assert cfg.load_config(root=p).sync.push == "async"  # the one real key applies


# ── legacy key alias window ───────────────────────────────────────────────────
def test_legacy_key_alias_honored_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    p = _proj(tmp_path)
    _legacy(p, "verify.require_verdict_for_close=true\n")  # legacy name
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        c = cfg.load_config(root=p)
    assert c.verify.require_signature_for_close is True  # aliased to canonical
    assert any("require_verdict_for_close" in r.getMessage() for r in caplog.records)


def test_canonical_wins_over_legacy_in_same_layer(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    # both keys in one file: canonical must win (legacy dropped)
    (p / "rebar.toml").write_text(
        "[verify]\nrequire_verdict_for_close = true\nrequire_signature_for_close = false\n",
        encoding="utf-8",
    )
    assert cfg.load_config(root=p).verify.require_signature_for_close is False


def test_legacy_alias_cross_layer_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 252e review property: a LEGACY key in a lower layer and the CANONICAL key
    in a higher layer must compare by their shared canonical name — the higher layer
    wins, because aliases resolve per-layer (to canonical) BEFORE the merge."""
    xdg = tmp_path / "xdg"
    (xdg / "rebar").mkdir(parents=True)
    # user (lower) sets the LEGACY key true
    (xdg / "rebar" / "config.toml").write_text(
        "[verify]\nrequire_verdict_for_close = true\n", encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    p = _proj(tmp_path)
    # project (higher) sets the CANONICAL key false → must win over the lower legacy
    (p / "rebar.toml").write_text(
        "[verify]\nrequire_signature_for_close = false\n", encoding="utf-8"
    )
    assert cfg.load_config(root=p).verify.require_signature_for_close is False


# ── unknown-key policy: WARN during window, ERROR past cutover ────────────────
def test_unknown_key_warns_during_window(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    p = _proj(tmp_path)
    _legacy(p, "sync.push=off\nsync.bogus=1\n")
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


def test_strict_does_not_error_on_known_or_aliased_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Strict mode hard-fails only TRULY unknown keys — a known key loads, and a
    deprecated-but-recognized legacy alias still WARNs (not errors)."""
    monkeypatch.setenv("REBAR_CONFIG_UNKNOWN_KEYS", "error")
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(
        "[verify]\nrequire_verdict_for_close = true\n[sync]\npush = 'off'\n", encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        c = cfg.load_config(root=p)
    assert c.verify.require_signature_for_close is True and c.sync.push == "off"
    assert any("require_verdict_for_close" in r.getMessage() for r in caplog.records)


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
