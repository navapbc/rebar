"""Config-driven status mapping — the S2 outbound FORWARD seam + reverse-map-free
round-trip (epic ravenous-dirt-widgeon / 438a-e1d9-29d3-4081).

Assertions target OBSERVABLE behaviour and contracts only — resolved mapping values,
the vendor-shaped mutation dicts the outbound mappers emit, the annotation-label
mutations, the local status the inbound recovery yields, and the fetcher's known-set —
never private structure. Every test drives a REAL entry point:

  * ``config.effective_status_map`` (the forward resolution)
  * ``OutboundFieldMapper.map_fields_to_remote`` (the shared UPDATE mapper, Cloud+DC)
  * ``outbound_labels._diff_status_annotation_labels`` (the stamp rule)
  * ``inbound_fields._map_jira_to_local_fields`` (project-key-free inbound recovery)
  * ``fetcher._known_jira_statuses`` (the snapshot-time known-set)

built-in behaviour (no ``[mapping]`` present) must stay bit-for-bit what it is today.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar import config as user_cfg
from rebar_reconciler import config as cfg_mod
from rebar_reconciler import fetcher, inbound_fields, outbound_labels
from rebar_reconciler.adapters.jira_family.outbound_mapper import OutboundFieldMapper

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures — isolated config discovery + a repo root carrying a [mapping] block
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No real user config may leak a ``[mapping]`` section into these tests."""
    xdg = tmp_path / "xdg"
    (xdg / "rebar").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    for name in ("REBAR_CONFIG", "REBAR_ROOT", "REBAR_CONFIG_UNKNOWN_KEYS"):
        monkeypatch.delenv(name, raising=False)
    user_cfg.set_cli_overrides(None)
    user_cfg.reset_config_cache()


def _proj(tmp: Path, mapping_toml: str = "", *, name: str = "proj") -> Path:
    """A repo root whose discovered ``rebar.toml`` carries ``mapping_toml`` verbatim."""
    p = tmp / name
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    (p / "rebar.toml").write_text(mapping_toml, encoding="utf-8")
    user_cfg.reset_config_cache()
    return p


class _NullCodec:
    """A minimal ``RichTextCodec`` — status mapping never touches rich text, so the
    description transforms are identity. Keeps these tests off the ADF/wiki machinery."""

    def fit_outbound(self, value: object) -> object:
        return value

    def normalize_outbound(self, value: object) -> object:
        return value

    def decode_inbound(self, value: object) -> object:
        return value


def _mapper() -> OutboundFieldMapper:
    return OutboundFieldMapper(_NullCodec())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# HAPPY PATH (visible to the implementer)
# ---------------------------------------------------------------------------


def test_status_config_effect(tmp_path: Path) -> None:
    """The effective forward map is ``local_to_jira_status`` <- ``default`` <-
    ``projects.<KEY>``, and a non-default overlay changes the outbound target THROUGH
    the real UPDATE mapper — not merely in the resolved dict.

    A key named in NO overlay still resolves to the built-in default, so a project that
    remaps one status leaves every other status exactly as it is today.
    """
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.status_map]\nin_progress = "Doing"\n',
    )

    eff = cfg_mod.effective_status_map("REB", root=proj)
    # the overlaid key is remapped ...
    assert eff["in_progress"] == "Doing"
    # ... and an unnamed key inherits the built-in default (unchanged behaviour)
    assert eff["open"] == cfg_mod.local_to_jira_status["open"]

    # And the REAL mapper emits the remapped target for the overlaid status ...
    out = _mapper().map_fields_to_remote({"status": "in_progress"}, status_map=eff)
    assert out["status"] == "Doing"
    # ... while an un-overlaid status is untouched.
    out2 = _mapper().map_fields_to_remote({"status": "open"}, status_map=eff)
    assert out2["status"] == cfg_mod.local_to_jira_status["open"]


def test_status_config_effect_builtin_default(tmp_path: Path) -> None:
    """With NO ``[mapping]`` block the effective map equals the built-in
    ``local_to_jira_status`` and the mapper's output is bit-for-bit today's — the
    config seam is inert until configured. (Happy path: the no-config invariant.)"""
    proj = _proj(tmp_path, "")
    eff = cfg_mod.effective_status_map("REB", root=proj)
    assert eff == {k: v for k, v in cfg_mod.local_to_jira_status.items()}

    out = _mapper().map_fields_to_remote({"status": "in_progress"}, status_map=eff)
    assert out["status"] == "In Progress"
    # And passing status_map=None falls back to the same built-in behaviour.
    out_none = _mapper().map_fields_to_remote({"status": "in_progress"}, status_map=None)
    assert out_none["status"] == "In Progress"


# ---------------------------------------------------------------------------
# HELD-OUT ORACLE — edge / contract / round-trip (withheld from the implementer)
# ---------------------------------------------------------------------------


def test_unmapped_status_drifts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A status with no effective target DRIFTS: the mutation OMITS the ``status``
    field (Jira left as-is) and a non-fatal warning is emitted — never a coerced
    ``"To Do"`` and never a per-mutation failure.

    ``SKIP`` in the overlay removes the built-in entry, so ``blocked`` has no target.
    """
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.status_map]\nblocked = "skip"\n',
    )
    eff = cfg_mod.effective_status_map("REB", root=proj)
    assert "blocked" not in eff  # SKIP is stripped from the effective map

    out = _mapper().map_fields_to_remote({"status": "blocked"}, status_map=eff)
    assert "status" not in out  # DRIFT: field omitted, Jira unchanged
    warn = capsys.readouterr().err
    assert "blocked" in warn  # a non-fatal drift alert names the status


def test_status_remap_roundtrip(tmp_path: Path) -> None:
    """A remapped status round-trips losslessly via a ``rebar-status:<local>`` label:
    because the remapped Jira target no longer reverse-maps to the local status, the
    stamp rule adds the label, and inbound recovers the local status FROM the label
    without any per-project reverse map or project key."""
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.status_map]\nin_progress = "Doing"\n',
    )
    eff = cfg_mod.effective_status_map("REB", root=proj)

    # OUTBOUND: the remap makes "Doing" non-canonical for in_progress -> stamp a label.
    muts = outbound_labels._diff_status_annotation_labels("in_progress", [], status_map=eff)
    assert {"action": "add", "label": "rebar-status:in_progress"} in muts

    # INBOUND (project-key-free): recover in_progress from the label even though the
    # raw workflow status "Doing" has no built-in reverse entry.
    recovered = inbound_fields._map_jira_to_local_fields(
        {"status": "Doing", "labels": ["rebar-status:in_progress"]}
    )
    assert recovered["status"] == "in_progress"


def test_status_collapse_roundtrip(tmp_path: Path) -> None:
    """A COLLAPSE (two locals -> one Jira status) still round-trips: both locals carry
    distinguishing ``rebar-status:`` labels, so inbound recovers each one exactly
    despite the shared workflow status."""
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.status_map]\nopen = "Active"\nin_progress = "Active"\n',
    )
    eff = cfg_mod.effective_status_map("REB", root=proj)

    open_muts = outbound_labels._diff_status_annotation_labels("open", [], status_map=eff)
    ip_muts = outbound_labels._diff_status_annotation_labels("in_progress", [], status_map=eff)
    assert {"action": "add", "label": "rebar-status:open"} in open_muts
    assert {"action": "add", "label": "rebar-status:in_progress"} in ip_muts

    assert (
        inbound_fields._map_jira_to_local_fields(
            {"status": "Active", "labels": ["rebar-status:open"]}
        )["status"]
        == "open"
    )
    assert (
        inbound_fields._map_jira_to_local_fields(
            {"status": "Active", "labels": ["rebar-status:in_progress"]}
        )["status"]
        == "in_progress"
    )


def test_fetcher_known_includes_config(tmp_path: Path) -> None:
    """A Jira status NAME introduced only by config is part of the fetcher's known-set,
    so it does not trip a spurious 'no reconciler mapping' warning. The known-set is the
    built-in reverse keys UNION every Jira status name declared across the config."""
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.status_map]\nin_progress = "Doing"\n',
    )
    known = fetcher._known_jira_statuses(repo_root=proj)
    assert "Doing" in known  # config-declared name is known
    assert "To Do" in known  # built-in names still present


def test_status_label_helper_single_source(tmp_path: Path) -> None:
    """The duplicated ``_REBAR_STATUS_LABEL_TO_LOCAL`` literal (blocked/cancelled only)
    is retired for a shared helper that recovers ANY ``rebar-status:<local>`` whose
    ``<local>`` is a valid local status — including one the old two-entry literal could
    never have expressed (e.g. ``open``). This is the behavioural proof the literal is
    gone: a generalised label is recovered at the inbound entry point."""
    recovered = inbound_fields._map_jira_to_local_fields(
        {"status": "Anything", "labels": ["rebar-status:open"]}
    )
    assert recovered["status"] == "open"
    # blocked/cancelled (the historical two) still work through the same helper.
    assert (
        inbound_fields._map_jira_to_local_fields(
            {"status": "In Progress", "labels": ["rebar-status:blocked"]}
        )["status"]
        == "blocked"
    )


# --- advisory-driven edge cases (folded from the round-7 plan review) ---


def test_drifted_status_emits_no_label(tmp_path: Path) -> None:
    """ADVISORY-1: a DRIFTED status (absent from the effective map) yields NO
    ``rebar-status:`` label and does not raise — the stamp rule must guard the map
    lookup, since a drifted local has no target to reverse-check."""
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.status_map]\nblocked = "skip"\n',
    )
    eff = cfg_mod.effective_status_map("REB", root=proj)
    assert "blocked" not in eff

    muts = outbound_labels._diff_status_annotation_labels("blocked", [], status_map=eff)
    added = [m for m in muts if m["action"] == "add"]
    assert added == []  # no label for a status with no target


def test_malformed_label_suffix_ignored() -> None:
    """ADVISORY-2: a ``rebar-status:<local>`` label whose ``<local>`` is NOT a valid
    local status is ignored (validation-fail branch), and recovery falls back to the
    raw workflow status rather than adopting the bogus suffix."""
    recovered = inbound_fields._map_jira_to_local_fields(
        {"status": "To Do", "labels": ["rebar-status:not_a_real_status"]}
    )
    assert recovered["status"] == "open"  # fell back to the workflow status


def test_preflight_tolerates_config_remap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The post-mapper status preflight must tolerate a config-effective Jira status
    NAME, not just built-in ones: a per-project REMAP makes the mapper emit a
    config-only Jira name, and the preflight (which runs AFTER the mapper) must not fire
    a spurious 'not mapped' warning for it — the same config-only false-alarm class the
    fetcher known-set fixes. A genuinely unmapped status still warns (negative control).
    """
    from rebar_reconciler import reconcile_helpers

    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.status_map]\nin_progress = "Doing"\n',
    )
    # A remapped Jira NAME the mapper legitimately emits post-remap.
    reconcile_helpers.preflight_status_mapping(
        [{"action": "update", "key": "REB-1", "fields": {"status": "Doing"}}],
        repo_root=proj,
    )
    err = capsys.readouterr().err
    assert "Doing" not in err  # config-declared name is tolerated, no false alarm

    # Negative control: a status declared NOWHERE still warns.
    reconcile_helpers.preflight_status_mapping(
        [{"action": "update", "key": "REB-2", "fields": {"status": "Nowhere"}}],
        repo_root=proj,
    )
    assert "Nowhere" in capsys.readouterr().err


# --- code-review-driven edge cases (folded from Gerrit 2018 patch set 2) ---


def test_effective_status_map_for_resolves_project(tmp_path: Path) -> None:
    """REVIEW (tests): the outbound_differ seam ``_effective_status_map_for`` — which
    threads the per-project effective status map into BOTH the CREATE and UPDATE paths —
    resolves the ticket's target project and returns its effective overlay. Observable
    contract: a ticket bound to a remapped project yields that project's remapped map;
    an unseeded/empty mapping yields ``None`` (the built-in fallback the mappers apply)."""
    from rebar_reconciler import outbound_differ
    from rebar_reconciler.projects_store import Mapping

    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.status_map]\nin_progress = "Doing"\n',
    )
    mapping = Mapping(projects={"REB": {"repos": []}})

    eff = outbound_differ._effective_status_map_for({"bridge_project": "REB"}, mapping, proj)
    assert eff is not None
    assert eff["in_progress"] == "Doing"

    # An empty/unseeded mapping short-circuits to None (built-in fallback).
    assert (
        outbound_differ._effective_status_map_for(
            {"bridge_project": "REB"}, Mapping(projects={}), proj
        )
        is None
    )


def test_effective_status_map_for_honors_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVIEW (correctness): ``_effective_status_map_for`` must discover ``[mapping]``
    from the PASSED ``repo_root``, never the process CWD — consistent with the sibling
    ``fetcher._known_jira_statuses`` / ``preflight_status_mapping``. A reconcile pass may
    run from a CWD that is not the store root, so a CWD-relative read would silently
    resolve the WRONG project's overlay."""
    from rebar_reconciler import outbound_differ
    from rebar_reconciler.projects_store import Mapping

    authoritative = _proj(
        tmp_path,
        '[mapping.projects.REB.status_map]\nin_progress = "Doing"\n',
        name="authoritative",
    )
    decoy = _proj(
        tmp_path,
        '[mapping.projects.REB.status_map]\nin_progress = "Wrong"\n',
        name="decoy",
    )
    monkeypatch.chdir(decoy)  # if the seam read CWD it would pick "Wrong"

    eff = outbound_differ._effective_status_map_for(
        {"bridge_project": "REB"}, Mapping(projects={"REB": {"repos": []}}), authoritative
    )
    assert eff is not None
    assert eff["in_progress"] == "Doing"  # from repo_root, not the decoy CWD


def test_recovery_vocab_covers_every_local_status() -> None:
    """REVIEW (maintainability): the inbound recovery vocabulary must not be a divergent
    hand-maintained literal — it must cover EVERY local status the outbound stamp rule
    can emit (the built-in forward-map domain) so no legitimately-stamped
    ``rebar-status:<local>`` is silently dropped inbound. Behavioural proof: each local
    status the forward map recognises round-trips through the real inbound recovery."""
    for local in cfg_mod.local_to_jira_status:
        recovered = inbound_fields._map_jira_to_local_fields(
            {"status": "Anything", "labels": [f"rebar-status:{local}"]}
        )
        assert recovered["status"] == local, f"vocab dropped a stampable status: {local}"


def test_drift_warning_deduped_within_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """REVIEW (maintainability): a persistently drifting status must not re-print its
    stderr drift alert on every mapper call within a pass — the warning is emitted at
    most once per distinct drifted status, so a stuck status does not flood the log each
    reconcile pass."""
    from rebar_reconciler.adapters.jira_family import outbound_mapper

    outbound_mapper.reset_drift_warnings()
    # Same drifted status resolved twice: exactly one warning line.
    for _ in range(3):
        assert outbound_mapper.resolve_outbound_status("blocked", {"open": "To Do"}) is None
    err = capsys.readouterr().err
    assert err.count("no Jira target") == 1
