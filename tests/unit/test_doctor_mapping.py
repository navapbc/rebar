"""``doctor`` mapping-config diagnostics — offline-always / live-drift-degrades
(epic ravenous-dirt-widgeon, story panphobic-prickly-xenarthra / 52b0-6a6a-c66b-4c59).

The mapping seam (S1-S8) adds a hand-editable ``[mapping]`` config surface. ``doctor``
must surface two failure classes before a reconcile silently drifts:

  * **internally-invalid config** — already fail-closed via ``MappingConfigError`` (a
    non-integer ``hierarchy`` / malformed block fails at LOAD; an out-of-vocabulary value
    or an unmapped-non-skipped type fails in the ``rebar_reconciler.config`` resolvers) —
    surfaced as **error** findings (non-zero exit);
  * **live drift** — internally valid but disagreeing with live Jira (a configured
    status/type/link value Jira no longer exposes) — surfaced when Jira is reachable and
    degraded to a single ``unavailable`` finding (zero exit) when it is not.

Every assertion targets OBSERVABLE behaviour and contracts only — the finding list
``scan_mapping`` returns (kind / severity / detail) and the exit code ``doctor_cli``
yields — never private structure. The check must stay PORTABLE: it runs in-process with
no live Jira and no specific CI provider (``project.portability``).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from rebar_reconciler import config as cfg_mod

from rebar import config as user_cfg
from rebar._commands import doctor, doctor_mapping

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures — isolated config discovery + a repo root carrying a [mapping] block
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No real user config may leak a ``[mapping]`` section (or a ``JIRA_PAT``) into
    these tests."""
    xdg = tmp_path / "xdg"
    (xdg / "rebar").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    for name in ("REBAR_CONFIG", "REBAR_ROOT", "REBAR_CONFIG_UNKNOWN_KEYS", "JIRA_PAT"):
        monkeypatch.delenv(name, raising=False)
    user_cfg.set_cli_overrides(None)
    user_cfg.reset_config_cache()


def _proj(tmp: Path, mapping_toml: str = "", *, name: str = "proj") -> Path:
    """A repo root whose discovered ``rebar.toml`` carries ``mapping_toml`` verbatim, and
    which is a git work tree so the rest of ``doctor`` (link/dirty/lock scans) runs."""
    p = tmp / name
    (p / ".git").mkdir(parents=True)
    (p / "rebar.toml").write_text(mapping_toml, encoding="utf-8")
    user_cfg.reset_config_cache()
    return p


def _severities(findings: list[dict]) -> list[str]:
    return [f["severity"] for f in findings]


def _by_severity(findings: list[dict], severity: str) -> list[dict]:
    return [f for f in findings if f["severity"] == severity]


# ===========================================================================
# HAPPY PATH (the only tier the implementer sees)
# ===========================================================================


def test_valid_config_offline_clean_live_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A VALID ``[mapping]`` config, with the ``jira-datacenter`` capability absent,
    produces NO error and NO warning findings — only a single ``unavailable`` live-drift
    finding — and contributes a zero exit. This is the portable, no-Jira happy path: the
    offline tier passes and the live-drift tier degrades cleanly.
    """
    monkeypatch.setattr(
        "rebar._optional.capability_installed",
        lambda key: False,
    )
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.status_map]\nin_progress = "Doing"\n',
    )

    findings = doctor_mapping.scan_mapping(str(proj))

    assert _by_severity(findings, "error") == []
    assert _by_severity(findings, "warning") == []
    unavailable = _by_severity(findings, "unavailable")
    assert len(unavailable) == 1
    assert not doctor_mapping.has_blocking_mapping(findings)


# ===========================================================================
# HELD-OUT ORACLE  (edge / E2E — withheld from the implementer)
# ===========================================================================


# --- offline error tier: both MappingConfigError message-prefix formats ----------------


def test_offline_load_error_axis_prefix_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LOAD-time failure in the ``[mapping.<where>]:`` message format (a non-integer
    ``hierarchy`` rank) surfaces as ONE error finding whose detail is the
    ``MappingConfigError`` message verbatim, and the per-key resolver pass is skipped.
    """
    monkeypatch.setattr("rebar._optional.capability_installed", lambda key: False)
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.hierarchy]\nEpic = "high"\n',
    )

    findings = doctor_mapping.scan_mapping(str(proj))

    errors = _by_severity(findings, "error")
    assert len(errors) == 1
    assert "[mapping.projects.REB.hierarchy]" in errors[0]["detail"]
    assert doctor_mapping.has_blocking_mapping(findings)


def test_offline_load_error_vocab_prefix_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LOAD-time failure in the OTHER message format — ``[mapping].<where>:`` (dot AFTER
    the bracket), emitted by vocabulary parsing — a malformed ``statuses`` declaration —
    surfaces as ONE error finding with the message verbatim. Both formats must surface;
    the implementation must not assume a single prefix shape.
    """
    monkeypatch.setattr("rebar._optional.capability_installed", lambda key: False)
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB]\nstatuses = "notalist"\n',
    )

    findings = doctor_mapping.scan_mapping(str(proj))

    errors = _by_severity(findings, "error")
    assert len(errors) == 1
    assert "[mapping].projects.REB.statuses" in errors[0]["detail"]
    assert doctor_mapping.has_blocking_mapping(findings)


# --- offline error tier: resolve-time errors -------------------------------------------


def test_offline_resolve_error_out_of_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RESOLVE-time failure — a ``status_map`` value outside a declared ``statuses``
    vocabulary — is reached by running the provider-neutral ``config`` resolvers per key
    and surfaces as an error finding naming the offending key.
    """
    monkeypatch.setattr("rebar._optional.capability_installed", lambda key: False)
    proj = _proj(
        tmp_path,
        "[mapping.projects.REB]\n"
        'statuses = ["To Do"]\n'
        "[mapping.projects.REB.status_map]\n"
        'open = "Nonexistent"\n',
    )

    findings = doctor_mapping.scan_mapping(str(proj))

    errors = _by_severity(findings, "error")
    assert len(errors) == 1
    # The resolve-time MappingConfigError message names the axis+value; the offending
    # project key is carried on the finding's ``key`` field (the per-key pass supplies it).
    assert errors[0]["key"] == "REB"
    assert "Nonexistent" in errors[0]["detail"]
    assert doctor_mapping.has_blocking_mapping(findings)


def test_offline_resolve_error_unmapped_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RESOLVE-time failure — a syncable ticket type with NO sync decision (simulated by
    monkeypatching the built-in ``LOCAL_TYPE_TO_JIRA`` to drop one entry, the documented
    hook) — is caught by ``assert_type_decisions_complete`` and surfaces as an error.
    """
    monkeypatch.setattr("rebar._optional.capability_installed", lambda key: False)
    trimmed = dict(cfg_mod.LOCAL_TYPE_TO_JIRA)
    trimmed.pop(next(iter(trimmed)))
    monkeypatch.setattr(cfg_mod, "LOCAL_TYPE_TO_JIRA", trimmed)
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.status_map]\nin_progress = "Doing"\n',
    )

    findings = doctor_mapping.scan_mapping(str(proj))

    assert _by_severity(findings, "error"), "an undecided syncable type must be an error"
    assert doctor_mapping.has_blocking_mapping(findings)


# --- offline warning tier: an all-empty stub project block -----------------------------


def test_offline_empty_project_block_is_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An all-empty ``[mapping.projects.<KEY>]`` block (a likely stub) is a genuinely SOFT
    problem — a **warning** naming the key, NOT an error and NOT a non-zero exit.
    """
    monkeypatch.setattr("rebar._optional.capability_installed", lambda key: False)
    proj = _proj(tmp_path, "[mapping.projects.STUB]\n")

    findings = doctor_mapping.scan_mapping(str(proj))

    assert _by_severity(findings, "error") == []
    warnings = _by_severity(findings, "warning")
    assert len(warnings) == 1
    assert "STUB" in warnings[0]["detail"]
    assert not doctor_mapping.has_blocking_mapping(findings)


# --- portability tier: the capability guard --------------------------------------------


def test_portability_guard_absent_extra_single_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the ``jira-datacenter`` extra absent (guard forced false) the live-drift tier
    emits exactly ONE ``unavailable`` finding, imports no ``adapters.jira`` module, and
    contributes a zero exit — the portability contract.
    """
    called: list[str] = []
    monkeypatch.setattr(
        "rebar._optional.capability_installed",
        lambda key: called.append(key) or False,
    )
    # If the guard were bypassed, the probe factory would be reached — make that fatal.
    from rebar_reconciler import mapping_probe

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("probe must not be built when the extra is absent")

    monkeypatch.setattr(mapping_probe, "build_probe", _boom, raising=False)
    proj = _proj(tmp_path, "")

    findings = doctor_mapping.scan_mapping(str(proj))

    assert "jira_datacenter" in called
    unavailable = _by_severity(findings, "unavailable")
    assert len(unavailable) == 1
    assert _by_severity(findings, "error") == []
    assert not doctor_mapping.has_blocking_mapping(findings)


# --- degradation causes: every cause folds into ONE unavailable ------------------------


def test_degradation_empty_pat_folds_to_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the extra present but ``JIRA_PAT`` unset, the reused ``build_probe`` reaches
    ``build_client_from_settings``, which raises ``BackendEnvError`` on the empty PAT —
    doctor must FOLD that into the single ``unavailable`` finding, never raise.
    """
    monkeypatch.setattr("rebar._optional.capability_installed", lambda key: True)
    # JIRA_PAT is already cleared by the autouse fixture; drive the REAL empty-PAT path
    # through the real probe reader (build_client_from_settings raises before any network).
    proj = _proj(tmp_path, "")

    findings = doctor_mapping.scan_mapping(str(proj))

    unavailable = _by_severity(findings, "unavailable")
    assert len(unavailable) == 1
    assert _by_severity(findings, "error") == []
    assert not doctor_mapping.has_blocking_mapping(findings)


def test_degradation_config_error_folds_to_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``ConfigError`` raised while resolving DC settings folds into ONE ``unavailable``
    finding, never raises.
    """
    monkeypatch.setattr("rebar._optional.capability_installed", lambda key: True)
    from rebar_reconciler import mapping_probe

    from rebar._config_coercion import ConfigError

    def _raise(*_a: object, **_k: object) -> object:
        raise ConfigError("malformed [tool.rebar.reconciler]")

    monkeypatch.setattr(mapping_probe, "build_probe", _raise, raising=False)
    proj = _proj(tmp_path, "")

    findings = doctor_mapping.scan_mapping(str(proj))

    assert len(_by_severity(findings, "unavailable")) == 1
    assert _by_severity(findings, "error") == []


def test_degradation_slow_probe_times_out_to_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hanging probe reader exceeding the doctor-side bounded wall-clock timeout is
    ABANDONED and folds into ONE ``unavailable`` finding — doctor returns promptly; the
    slow host never blocks it.
    """
    monkeypatch.setattr("rebar._optional.capability_installed", lambda key: True)
    monkeypatch.setattr(doctor_mapping, "_PROBE_TIMEOUT_S", 0.2, raising=False)
    from rebar_reconciler import mapping_probe

    def _hang(*_a: object, **_k: object) -> object:
        time.sleep(5.0)
        raise AssertionError("should have been abandoned")

    monkeypatch.setattr(mapping_probe, "build_probe", _hang, raising=False)
    proj = _proj(tmp_path, "")

    start = time.monotonic()
    findings = doctor_mapping.scan_mapping(str(proj))
    elapsed = time.monotonic() - start

    # timing: hang-guard — 4s ceiling dwarfs the 0.2s timeout, proving the 5s sleep was abandoned
    assert elapsed < 4.0, "doctor must not block on a hanging probe"
    assert len(_by_severity(findings, "unavailable")) == 1
    assert _by_severity(findings, "error") == []


# --- live-drift tier: injected transport -----------------------------------------------


class _DivergentPort:
    """An offline stand-in probe port whose observed vocabulary is the built-in target
    value set — so only the deliberately-remapped bogus values diverge."""

    def issue_types(self) -> list[dict]:
        return [{"name": v} for v in set(cfg_mod.LOCAL_TYPE_TO_JIRA.values())]

    def statuses(self) -> list[str]:
        return list(set(cfg_mod.local_to_jira_status.values()))

    def issue_link_types(self) -> list[str]:
        return list(set(cfg_mod.local_to_jira_link.values()))

    def priorities(self) -> list[str]:
        return list(set(cfg_mod.local_to_jira_priority.values()))


def test_live_drift_per_axis_divergence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a fake probe reader injected, each configured value that is ABSENT from Jira's
    observed vocabulary — on status, type, and link — yields a drift finding naming the
    axis and the absent value. (Hierarchy is excluded by design: the reused Data Center
    probe never observes ``hierarchyLevel``.)
    """
    monkeypatch.setattr("rebar._optional.capability_installed", lambda key: True)
    from rebar_reconciler import mapping_probe

    monkeypatch.setattr(
        mapping_probe, "build_probe", lambda *a, **k: _DivergentPort(), raising=False
    )
    proj = _proj(
        tmp_path,
        "[mapping.projects.REB.status_map]\n"
        'open = "BogusStatus"\n'
        "[mapping.projects.REB.type_map]\n"
        'story = "BogusType"\n'
        "[mapping.projects.REB.link_map]\n"
        'relates_to = "BogusLink"\n',
    )

    findings = doctor_mapping.scan_mapping(str(proj))

    drift = [f for f in findings if f.get("kind") == "mapping-drift"]
    axes = {f["axis"]: f["value"] for f in drift}
    assert axes.get("status") == "BogusStatus"
    assert axes.get("type") == "BogusType"
    assert axes.get("link") == "BogusLink"
    assert all(f["severity"] == "error" for f in drift)
    assert doctor_mapping.has_blocking_mapping(findings)


class _LinkAxisFailedPort:
    """A probe port whose CORE axes (statuses / issue types) succeed but whose link-types
    read COULD NOT CHECK — the richer probe contract signals that as ``None``, a
    different value from a legitimately-empty ``[]`` (Flutter doctor's ``notAvailable`` /
    Homebrew's ``T.nilable(Finding)`` precedent)."""

    def issue_types(self) -> list[dict]:
        return [{"name": v} for v in set(cfg_mod.LOCAL_TYPE_TO_JIRA.values())]

    def statuses(self) -> list[str]:
        return list(set(cfg_mod.local_to_jira_status.values()))

    def issue_link_types(self) -> list[str] | None:
        return None


class _LinkAxisEmptyPort(_LinkAxisFailedPort):
    """A probe port whose link-types read SUCCEEDED and observed nothing — a genuinely
    empty vocabulary, which MUST keep producing drift errors for configured link values."""

    def issue_link_types(self) -> list[str] | None:
        return []


def test_link_probe_failure_degrades_link_axis_not_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PARTIAL probe failure — link-types could not be checked (``None``) while
    statuses/types succeeded — must NOT report every configured link target as drift.
    The link axis degrades to a distinct could-not-check ``unavailable`` finding, the
    other axes are still diffed (a bogus status still errors), and the link-axis
    degradation alone never blocks."""
    monkeypatch.setattr("rebar._optional.capability_installed", lambda key: True)
    from rebar_reconciler import mapping_probe

    monkeypatch.setattr(
        mapping_probe, "build_probe", lambda *a, **k: _LinkAxisFailedPort(), raising=False
    )
    proj = _proj(
        tmp_path,
        "[mapping.projects.REB.status_map]\n"
        'open = "BogusStatus"\n'
        "[mapping.projects.REB.link_map]\n"
        'relates_to = "Relates"\n',
    )

    findings = doctor_mapping.scan_mapping(str(proj))

    drift = [f for f in findings if f.get("kind") == "mapping-drift"]
    assert [f["axis"] for f in drift if f["axis"] == "link"] == []
    assert any(f["axis"] == "status" and f["value"] == "BogusStatus" for f in drift)
    axis_unavailable = [
        f for f in findings if f["severity"] == "unavailable" and f.get("axis") == "link"
    ]
    assert len(axis_unavailable) == 1
    assert "could not check" in axis_unavailable[0]["detail"]


def test_link_probe_failure_alone_is_not_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an otherwise-clean config, a link-types could-not-check degrades to the
    distinct ``unavailable`` finding and contributes NO error — zero-exit territory."""
    monkeypatch.setattr("rebar._optional.capability_installed", lambda key: True)
    from rebar_reconciler import mapping_probe

    monkeypatch.setattr(
        mapping_probe, "build_probe", lambda *a, **k: _LinkAxisFailedPort(), raising=False
    )
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.link_map]\nrelates_to = "Relates"\n',
    )

    findings = doctor_mapping.scan_mapping(str(proj))

    assert _by_severity(findings, "error") == []
    assert not doctor_mapping.has_blocking_mapping(findings)
    assert any(f.get("axis") == "link" for f in _by_severity(findings, "unavailable"))


def test_genuinely_empty_link_vocabulary_still_drifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link-types read that SUCCEEDED and observed an empty vocabulary (``[]``) is a
    valid observation, NOT a degradation: a configured link value must still surface as
    a drift error. Guards the checked-and-empty vs could-not-check distinction."""
    monkeypatch.setattr("rebar._optional.capability_installed", lambda key: True)
    from rebar_reconciler import mapping_probe

    monkeypatch.setattr(
        mapping_probe, "build_probe", lambda *a, **k: _LinkAxisEmptyPort(), raising=False
    )
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.link_map]\nrelates_to = "Relates"\n',
    )

    findings = doctor_mapping.scan_mapping(str(proj))

    drift = [f for f in findings if f.get("kind") == "mapping-drift"]
    assert any(f["axis"] == "link" and f["value"] == "Relates" for f in drift)
    assert doctor_mapping.has_blocking_mapping(findings)


# ===========================================================================
# EXIT-CODE FOLDING through the real doctor_cli entry point
# ===========================================================================


def test_doctor_cli_mapping_error_yields_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An offline mapping ERROR makes ``rebar doctor`` exit non-zero, folded into the exit
    code exactly as a stale lock or an unrepaired link finding is.
    """
    monkeypatch.setattr("rebar._optional.capability_installed", lambda key: False)
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.hierarchy]\nEpic = "high"\n',
    )

    rc = doctor.doctor_cli([], repo_root=str(proj))

    assert rc == 1
    out = capsys.readouterr().out
    assert "mapping" in out


def test_doctor_cli_mapping_warning_only_yields_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mapping WARNING (stub block) and an ``unavailable`` live-drift finding — with no
    error — leave ``rebar doctor`` at a zero exit, while still being rendered.
    """
    monkeypatch.setattr("rebar._optional.capability_installed", lambda key: False)
    proj = _proj(tmp_path, "[mapping.projects.STUB]\n")

    rc = doctor.doctor_cli([], repo_root=str(proj))

    assert rc == 0
    out = capsys.readouterr().out
    assert "STUB" in out
