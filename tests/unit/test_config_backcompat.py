"""Config unknown-key policy: unknown keys WARN during the deprecation window and
HARD-ERROR past the cutover (``REBAR_CONFIG_UNKNOWN_KEYS=error``), with cross-layer
precedence preserved.

(The legacy flat ``.rebar/config.conf`` reader and the ``verify.require_verdict_for_close``
alias were removed pre-1.0 — DE7 — so config now loads only from ``rebar.toml`` / a
``[tool.rebar]`` pyproject table, and the old alias is just an unknown key now.)
"""

from __future__ import annotations

import logging
import re
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
def test_removed_verdict_key_is_a_load_bearing_tombstone(tmp_path: Path) -> None:
    """``verify.require_verdict_for_close`` is a load-bearing TOMBSTONE (story 36c7):
    the removed close-gate key still set in config must FAIL LOUD with a targeted
    RemovedInputError naming its replacement — not be silently ignored as an unknown
    key (its removal changes close-gate semantics, so a silent drop is unsafe)."""
    from rebar._deprecations import RemovedInputError

    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[verify]\nrequire_verdict_for_close = true\n", encoding="utf-8")
    with pytest.raises(RemovedInputError, match="require_completion_verification_for_close"):
        cfg.load_config(root=p)


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


# ── removed keys: the three settled verify off-switches are now unknown keys ───
@pytest.mark.parametrize(
    "removed_key",
    ["progressive_drift_refresh", "remediation_mode", "novelty_drop_active"],
)
def test_removed_settled_verify_switches_ignored_and_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, removed_key: str
) -> None:
    """The three settled verify defaults (`progressive_drift_refresh`,
    `remediation_mode`, `novelty_drop_active`) were made always-on and their off
    switches removed (story 4cdf). Each is now just an unknown key (warned +
    ignored), and `VerifyConfig` no longer carries the attribute. The retained
    tuning params (`remediation_window_minutes`, `novelty_drop_threshold`,
    `novelty_priority_floor`) are unaffected — see test_config_typed."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(f"[verify]\n{removed_key} = false\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        c = cfg.load_config(root=p)
    assert not hasattr(c.verify, removed_key)
    assert any(removed_key in r.getMessage() for r in caplog.records)


# ── removed key: compact.emit_legacy_signature_mirror is now an unknown key ────
def test_removed_legacy_signature_mirror_key_ignored_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`compact.emit_legacy_signature_mirror` (the CONTRACT-phase rollback lever of
    the additive-attestations rollout) was retired (story 7ed9): new snapshots never
    persist the legacy `signature` mirror. The key is now just an unknown key (warned
    + ignored), and `CompactConfig` no longer carries the attribute."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(
        "[compact]\nemit_legacy_signature_mirror = true\n", encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        c = cfg.load_config(root=p)
    assert not hasattr(c.compact, "emit_legacy_signature_mirror")
    assert any("emit_legacy_signature_mirror" in r.getMessage() for r in caplog.records)


# ── removed keys: the two reconciler baseline rollout flags are now unknown ────
@pytest.mark.parametrize("removed_key", ["baseline_dual_write", "baseline_consumer_swap"])
def test_removed_reconciler_baseline_flags_ignored_and_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, removed_key: str
) -> None:
    """The two convergence-rollout phase flags `reconciler.baseline_dual_write` and
    `baseline_consumer_swap` were retired and hardcoded always-on (story d6bd). Each
    is now just an unknown key (warned + ignored), and `ReconcilerConfig` no longer
    carries the attribute."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(f"[reconciler]\n{removed_key} = false\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        c = cfg.load_config(root=p)
    assert not hasattr(c.reconciler, removed_key)
    assert any(removed_key in r.getMessage() for r in caplog.records)


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
    with pytest.raises(cfg.ConfigError, match=r"sync.bogus"):
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


def test_removed_verdict_key_tombstone_fires_regardless_of_unknown_key_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``require_verdict_for_close`` TOMBSTONE (story 36c7) fires as a
    RemovedInputError independent of the unknown-key policy — the tombstone path is
    checked BEFORE the generic unknown-key path, so even the lenient (default) policy
    fails loud on this load-bearing removed key."""
    from rebar._deprecations import RemovedInputError

    monkeypatch.setenv("REBAR_CONFIG_UNKNOWN_KEYS", "error")
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[verify]\nrequire_verdict_for_close = true\n", encoding="utf-8")
    with pytest.raises(RemovedInputError, match="require_verdict_for_close"):
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


# ── removed key: the inert code-health off switch (bug a573-00ea-2bf6-4eb1) ────
def test_removed_code_health_enabled_cfg_key_warns_like_any_unknown_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`code_health.enabled` was removed: it had ZERO read sites, so it never
    switched the structural metrics off. It carried a warn-class tombstone until the
    warn class was retired; it is now an ordinary unknown key, so the operator still
    learns the line is dead, through the generic channel.

    The surviving keys in the same section must keep applying, which is what proves
    the section still coerces rather than being rejected wholesale."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(
        "[code_health]\nenabled = true\nsize_cap = 800\n", encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING):
        c = cfg.load_config(root=p)

    assert not hasattr(c.code_health, "enabled")
    assert c.code_health.size_cap == 800  # the surviving keys still apply
    assert any(
        "code_health.enabled" in r.getMessage() and "unknown key" in r.getMessage()
        for r in caplog.records
    ), f"expected an unknown-key warning, got: {[r.getMessage() for r in caplog.records]}"


def test_removed_code_health_enabled_cfg_key_obeys_strict_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deliberate behaviour change from retiring the warn class: the dead key no
    longer gets a tombstone exemption from `REBAR_CONFIG_UNKNOWN_KEYS=error`. An
    operator who opted into strict now gets a hard failure naming it."""
    monkeypatch.setenv("REBAR_CONFIG_UNKNOWN_KEYS", "error")
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[code_health]\nenabled = true\n", encoding="utf-8")

    with pytest.raises(cfg.ConfigError, match=re.escape("code_health.enabled")):
        cfg.load_config(root=p)


def test_removed_code_health_enabled_env_var_is_silently_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The `REBAR_CODE_HEALTH_ENABLED` env twin is not read at all: `env_overrides()`
    only consults live section/key pairs, so a stale export cannot reach any code
    path. With no tombstone row there is nothing to announce, and announcing it would
    require re-enrolling a name that no longer exists in the schema."""
    monkeypatch.setenv("REBAR_CODE_HEALTH_ENABLED", "true")
    p = _proj(tmp_path)

    with caplog.at_level(logging.WARNING):
        c = cfg.load_config(root=p)

    assert not hasattr(c.code_health, "enabled")
    assert not any("REBAR_CODE_HEALTH_ENABLED" in r.getMessage() for r in caplog.records)


# ── reserved [llm] section: unknown keys get the same policy as core keys ─────
def test_unknown_llm_key_warns_like_a_core_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`[llm]` is a RESERVED section — owned by the optional agents layer, so the core
    parser never coerces it. That exemption used to extend to unknown-key policy, so a
    typo or a key a later story removed was accepted in total silence. The operator now
    gets the same warning they would for any other section."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[llm]\nbogus = 1\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        cfg.load_config(root=p)  # must NOT raise — reserved section still loads

    assert any(
        "llm.bogus" in r.getMessage() and "unknown key" in r.getMessage() for r in caplog.records
    ), f"expected an unknown-key warning, got: {[r.getMessage() for r in caplog.records]}"


def test_unknown_llm_key_errors_under_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half that the overlap epic needs: once it removes the `overlap_*` keys, a
    config still setting one must be able to fail loud rather than be ignored."""
    monkeypatch.setenv("REBAR_CONFIG_UNKNOWN_KEYS", "error")
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[llm]\noverlap_k = 5\n", encoding="utf-8")

    with pytest.raises(cfg.ConfigError, match=re.escape("llm.overlap_k")):
        cfg.load_config(root=p)


def test_known_llm_key_never_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The other half: every key the agents layer actually reads must stay silent, under
    strict too. Without this the section's own live config would start failing."""
    monkeypatch.setenv("REBAR_CONFIG_UNKNOWN_KEYS", "error")
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text(
        "[llm]\nmodel = 'x'\ntimeout = 30\nmax_steps = 4\n"
        "[llm.model_classes]\n[llm.evals]\ncorpus_root = 'c'\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        cfg.load_config(root=p)  # strict, yet must NOT raise

    assert not any("unknown key" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]
