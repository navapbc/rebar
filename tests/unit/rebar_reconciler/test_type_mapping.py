"""Config-driven type mapping + type-granular skip — the S3 outbound FORWARD seam,
load-time fail-closed, and reverse-map-free ``rebar-type:`` round-trip
(epic ravenous-dirt-widgeon / a8ea-be28-2394-4f0c).

Assertions target OBSERVABLE behaviour and contracts only — resolved mapping values,
the effective excluded set, the vendor-shaped ``issuetype`` the outbound CREATE mapper
emits, the annotation-label mutations, the local type inbound recovery yields, and the
fail-closed exception raised at the mutation entry point — never private structure.
Every test drives a REAL entry point:

  * ``config.effective_type_map`` (the forward resolution)
  * ``config.effective_excluded_sync_types`` / ``config.assert_type_decisions_complete``
  * ``JiraBackend.map_local_to_remote`` (the shared CREATE mapper, all three impls)
  * ``outbound_labels._diff_type_annotation_labels`` (the stamp rule)
  * ``inbound_fields.recover_type_label`` / ``_map_jira_to_local_fields`` (recovery)
  * ``outbound_differ.compute_outbound_mutations`` (the up-front fail-closed gate)

built-in behaviour (no ``[mapping]`` present) must stay bit-for-bit what it is today:
``LOCAL_TYPE_TO_JIRA`` = {bug: Bug, story: Story, task: Task, epic: Epic}.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

import pytest

from rebar import config as user_cfg
from rebar.types import TicketType
from rebar_reconciler import config as cfg_mod
from rebar_reconciler import inbound_fields, outbound_labels
from rebar_reconciler.adapters.jira_family.value_maps import LOCAL_TYPE_TO_JIRA

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
    """A minimal ``RichTextCodec`` — type mapping never touches rich text."""

    def fit_outbound(self, value: object) -> object:
        return value

    def normalize_outbound(self, value: object) -> object:
        return value

    def decode_inbound(self, value: object) -> object:
        return value


def _backend() -> Any:
    from rebar_reconciler.adapters.jira.backend import JiraBackend

    return JiraBackend(_NullCodec())  # type: ignore[arg-type]


def _issuetype(out: dict[str, Any]) -> Any:
    """The vendor ``issuetype`` the CREATE mapper emitted (name string)."""
    return out.get("issuetype")


# ---------------------------------------------------------------------------
# HAPPY PATH (visible to the implementer)
# ---------------------------------------------------------------------------


def test_effective_type_map_default_equals_builtin(tmp_path: Path) -> None:
    """With NO ``[mapping]`` block the effective type map equals the built-in
    ``LOCAL_TYPE_TO_JIRA`` verbatim, and a per-project ``type_map`` overlay remaps a
    type while leaving every un-overlaid type at its built-in target — the config seam
    is inert until configured. (Happy path: the no-config invariant + one remap.)"""
    base = _proj(tmp_path, "", name="base")
    eff = cfg_mod.effective_type_map("REB", root=base)
    assert eff == dict(LOCAL_TYPE_TO_JIRA)

    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.type_map]\nbug = "Defect"\n',
        name="over",
    )
    eff2 = cfg_mod.effective_type_map("REB", root=proj)
    assert eff2["bug"] == "Defect"  # overlaid key remapped
    assert eff2["story"] == LOCAL_TYPE_TO_JIRA["story"]  # unnamed key unchanged


def test_type_map_threaded_through_all_three_mappers(tmp_path: Path) -> None:
    """The effective type map reaches the REAL CREATE mapper: ``map_local_to_remote``
    accepts a ``type_map=`` kwarg (beside ``status_map``) and emits the remapped
    ``issuetype``. Passing ``type_map=None`` falls back to today's built-in behaviour.
    (Happy path drives the Cloud backend; the parity census covers all three impls.)"""
    proj = _proj(tmp_path, '[mapping.projects.REB.type_map]\nbug = "Defect"\n')
    eff = cfg_mod.effective_type_map("REB", root=proj)

    out = _backend().map_local_to_remote({"ticket_type": "bug"}, type_map=eff)
    assert _issuetype(out) == "Defect"

    out_builtin = _backend().map_local_to_remote({"ticket_type": "bug"}, type_map=None)
    assert _issuetype(out_builtin) == "Bug"


# ---------------------------------------------------------------------------
# HELD-OUT ORACLE — edge / contract / round-trip (withheld from the implementer)
# ---------------------------------------------------------------------------


def test_skip_is_a_valid_type_decision(tmp_path: Path) -> None:
    """A type mapped to ``SKIP`` is a DECIDED type: it passes the completeness gate, is
    a member of ``effective_excluded_sync_types``, and is ABSENT from the effective
    forward map (no Jira target). SKIP is the type-granular skip signal — S1's sentinel
    reused, not a separate axis."""
    proj = _proj(tmp_path, '[mapping.projects.REB.type_map]\nbug = "skip"\n')

    eff = cfg_mod.effective_type_map("REB", root=proj)
    assert "bug" not in eff  # SKIP stripped from the effective forward map

    excluded = cfg_mod.effective_excluded_sync_types("REB", root=proj)
    assert "bug" in excluded

    # The completeness gate tolerates SKIP as a decision (does not raise).
    cfg_mod.assert_type_decisions_complete("REB", root=proj)


def test_effective_excluded_union_builtin_and_skip(tmp_path: Path) -> None:
    """``effective_excluded_sync_types`` == the built-in ``EXCLUDED_SYNC_TYPES`` UNION
    every local type mapped to ``SKIP`` for the project. With no overlay it is exactly
    the built-in set; a SKIP adds ON TOP of it, never replacing it."""
    base = _proj(tmp_path, "", name="base")
    assert cfg_mod.effective_excluded_sync_types("REB", root=base) == set(
        cfg_mod.EXCLUDED_SYNC_TYPES
    )

    proj = _proj(tmp_path, '[mapping.projects.REB.type_map]\nepic = "skip"\n', name="ov")
    got = cfg_mod.effective_excluded_sync_types("REB", root=proj)
    assert got == set(cfg_mod.EXCLUDED_SYNC_TYPES) | {"epic"}


def test_fail_closed_fires_before_mutation_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An UNDECIDED syncable type (neither a Jira target nor SKIP) raises
    ``MappingConfigError`` from ``compute_outbound_mutations`` UP-FRONT — before any
    per-ticket mutation is built. Simulated by removing a built-in target so the type
    becomes undecided; the gate fires over the distinct project keys before the loop."""
    from rebar_reconciler import mapping_config, outbound_differ

    proj = _proj(tmp_path, "")
    # Drop a built-in target so 'bug' is a syncable type with no decision anywhere.
    patched = {k: v for k, v in LOCAL_TYPE_TO_JIRA.items() if k != "bug"}
    monkeypatch.setattr(cfg_mod, "LOCAL_TYPE_TO_JIRA", patched, raising=False)
    monkeypatch.setattr(
        "rebar_reconciler.adapters.jira_family.value_maps.LOCAL_TYPE_TO_JIRA",
        patched,
        raising=False,
    )

    with pytest.raises(mapping_config.MappingConfigError):
        cfg_mod.assert_type_decisions_complete("REB", root=proj)

    # And the gate is wired into the mutation entry point: a well-formed local ticket
    # that WOULD create a mutation raises before any mutation dict is produced.
    from rebar_reconciler.projects_store import Mapping

    with pytest.raises(mapping_config.MappingConfigError):
        outbound_differ.compute_outbound_mutations(
            [{"local_id": "T-1", "ticket_type": "bug", "bridge_project": "REB"}],
            {},
            mapping=Mapping(projects={"REB": {"repos": []}}),
            repo_root=proj,
        )


def test_type_skip_excludes_ticket_both_consumers(tmp_path: Path) -> None:
    """A local type mapped to ``SKIP`` is excluded from outbound sync at BOTH
    consumers of the effective excluded set — the per-ticket mutation loop AND the
    absent-gets selector — so a SKIP-typed ticket produces no create/update mutation
    and is not selected as an absent GET. Observable: no mutation references it."""
    from rebar_reconciler import outbound_differ
    from rebar_reconciler.projects_store import Mapping

    proj = _proj(tmp_path, '[mapping.projects.REB.type_map]\nbug = "skip"\n')
    mutations, _absent_alive_fields = outbound_differ.compute_outbound_mutations(
        [{"local_id": "T-1", "ticket_type": "bug", "bridge_project": "REB", "title": "x"}],
        {},
        mapping=Mapping(projects={"REB": {"repos": []}}),
        repo_root=proj,
    )
    # No create/update mutation is produced for a SKIP-excluded type.
    assert all(m.get("local_id") != "T-1" for m in mutations)


def test_rebar_type_label_stamped_when_lossy(tmp_path: Path) -> None:
    """A LOSSY (collapsing) type map stamps a ``rebar-type:<local>`` annotation label so
    the round-trip is recoverable: two locals collapsing to one Jira type each carry a
    distinguishing label, added via ``config.jira_to_local_type`` (the reverse check).
    A non-lossy built-in type that reverses to itself is NOT stamped."""
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.type_map]\nstory = "Task"\ntask = "Task"\n',
    )
    eff = cfg_mod.effective_type_map("REB", root=proj)

    story_muts = outbound_labels._diff_type_annotation_labels("story", [], type_map=eff)
    task_muts = outbound_labels._diff_type_annotation_labels("task", [], type_map=eff)
    assert {"action": "add", "label": "rebar-type:story"} in story_muts
    assert {"action": "add", "label": "rebar-type:task"} in task_muts

    # A type whose target reverses canonically to itself needs no label.
    base = _proj(tmp_path, "", name="base")
    eff0 = cfg_mod.effective_type_map("REB", root=base)
    bug_muts = outbound_labels._diff_type_annotation_labels("bug", [], type_map=eff0)
    assert [m for m in bug_muts if m["action"] == "add"] == []


def test_rebar_type_label_recovered_reverse_map_free(tmp_path: Path) -> None:
    """Inbound recovers the local type from the ``rebar-type:<local>`` label WITHOUT any
    per-project reverse map or project key — even when the raw Jira issuetype ("Task")
    reverse-maps to a DIFFERENT built-in local ("task"). The label wins over the raw
    type. ``recover_type_label`` is the shared recovery helper."""
    recovered = inbound_fields.recover_type_label(["rebar-type:story"])
    assert recovered == "story"

    fields = inbound_fields._map_jira_to_local_fields(
        {"issuetype": "Task", "labels": ["rebar-type:story"]}
    )
    assert fields["ticket_type"] == "story"

    # A malformed suffix (not a valid local type) is ignored; recovery is None.
    assert inbound_fields.recover_type_label(["rebar-type:not_a_type"]) is None


def test_rebar_type_prefix_excluded_all_four_tuples() -> None:
    """The ``rebar-type:`` prefix is a bridge-internal annotation and MUST be filtered by
    all four independent prefix tuples, so a stamped label never leaks into a user-facing
    label set on either the outbound or inbound path."""
    from rebar_reconciler import (
        differ,
        inbound_collection_diffs,
        inbound_translate,
    )
    from rebar_reconciler import (
        outbound_labels as ol,
    )

    assert any("rebar-type:" == p for p in ol._EXCLUDED_PREFIXES)
    assert any("rebar-type:" == p for p in inbound_collection_diffs._EXCLUDED_PREFIXES)
    assert any("rebar-type:" == p for p in differ._BRIDGE_INTERNAL_LABEL_PREFIXES)
    assert any("rebar-type:" == p for p in inbound_translate._BRIDGE_INTERNAL_TAG_PREFIXES)


def test_jira_to_local_type_parity() -> None:
    """``config.jira_to_local_type`` (the reverse map the stamp rule consults) must stay
    in lock-step with ``inbound_fields._JIRA_TO_LOCAL_TYPE`` — the parity that keeps the
    two independent literals honest, exactly as ``jira_to_local_status`` does."""
    assert cfg_mod.jira_to_local_type == inbound_fields._JIRA_TO_LOCAL_TYPE


def test_local_type_vocab_derived_from_ticket_type() -> None:
    """The inbound recovery vocabulary is DERIVED from ``get_args(TicketType)`` — never a
    divergent hand-maintained literal — so every local type the outbound stamp rule can
    emit round-trips, and no legitimately-stamped ``rebar-type:<local>`` is dropped."""
    assert set(inbound_fields._LOCAL_TYPE_VOCAB) == set(get_args(TicketType))
    for local in get_args(TicketType):
        assert inbound_fields.recover_type_label([f"rebar-type:{local}"]) == local
