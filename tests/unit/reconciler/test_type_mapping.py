"""Project type mapping drives outbound create, SKIP decisions, and inbound recovery.

No mapping preserves the built-in bug, story, task, and epic targets. An
undecided syncable type fails before mutation. Lossy mappings stamp a
rebar-type label so inbound recovery needs no project reverse map."""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

import pytest

from rebar import config as user_cfg
from rebar.reconciler import config as cfg_mod
from rebar.reconciler import inbound_fields, outbound_labels
from rebar.reconciler.adapters.jira_family.value_maps import LOCAL_TYPE_TO_JIRA
from rebar.types import TicketType

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
    from rebar.reconciler.adapters.jira.backend import JiraBackend

    return JiraBackend(_NullCodec())  # type: ignore[arg-type]


def _issuetype(out: dict[str, Any]) -> Any:
    """The vendor ``issuetype`` the CREATE mapper emitted (name string)."""
    return out.get("issuetype")


# ---------------------------------------------------------------------------
# HAPPY PATH (visible to the implementer)
# ---------------------------------------------------------------------------


def test_effective_type_map_default_equals_builtin(tmp_path: Path) -> None:
    """The built-in map applies by default. An overlay changes selected types."""
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
    """The effective type map reaches the create mapper and changes its Jira issuetype."""
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
    """SKIP satisfies completeness, joins the excluded set, and has no Jira target."""
    proj = _proj(tmp_path, '[mapping.projects.REB.type_map]\nbug = "skip"\n')

    eff = cfg_mod.effective_type_map("REB", root=proj)
    assert "bug" not in eff  # SKIP stripped from the effective forward map

    excluded = cfg_mod.effective_excluded_sync_types("REB", root=proj)
    assert "bug" in excluded

    # The completeness gate tolerates SKIP as a decision (does not raise).
    cfg_mod.assert_type_decisions_complete("REB", root=proj)


def test_effective_excluded_union_builtin_and_skip(tmp_path: Path) -> None:
    """Effective exclusions unite built-in excluded types with project SKIP mappings."""
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
    """An undecided syncable type raises MappingConfigError before any ticket mutation is built."""
    from rebar.reconciler import mapping_config, outbound_differ

    proj = _proj(tmp_path, "")
    # Drop a built-in target so 'bug' is a syncable type with no decision anywhere.
    patched = {k: v for k, v in LOCAL_TYPE_TO_JIRA.items() if k != "bug"}
    monkeypatch.setattr(cfg_mod, "LOCAL_TYPE_TO_JIRA", patched, raising=False)
    monkeypatch.setattr(
        "rebar.reconciler.adapters.jira_family.value_maps.LOCAL_TYPE_TO_JIRA",
        patched,
        raising=False,
    )

    with pytest.raises(mapping_config.MappingConfigError):
        cfg_mod.assert_type_decisions_complete("REB", root=proj)

    # And the gate is wired into the mutation entry point: a well-formed local ticket
    # that WOULD create a mutation raises before any mutation dict is produced.
    from rebar.reconciler.projects_store import Mapping

    with pytest.raises(mapping_config.MappingConfigError):
        outbound_differ.compute_outbound_mutations(
            [{"local_id": "T-1", "ticket_type": "bug", "bridge_project": "REB"}],
            {},
            mapping=Mapping(projects={"REB": {"repos": []}}),
            repo_root=proj,
        )


def test_type_skip_excludes_ticket_both_consumers(tmp_path: Path) -> None:
    """SKIP excludes a ticket from the mutation loop and absent-gets selector."""
    from rebar.reconciler import outbound_differ
    from rebar.reconciler.projects_store import Mapping

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
    """A collapsing type map stamps rebar-type:<local> so each local type can round-trip."""
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
    """Inbound recovers the local type from its rebar-type label without a project reverse map."""
    recovered = inbound_fields.recover_type_label(["rebar-type:story"])
    assert recovered == "story"

    fields = inbound_fields._map_jira_to_local_fields(
        {"issuetype": "Task", "labels": ["rebar-type:story"]}
    )
    assert fields["ticket_type"] == "story"

    # A malformed suffix (not a valid local type) is ignored; recovery is None.
    assert inbound_fields.recover_type_label(["rebar-type:not_a_type"]) is None


def test_rebar_type_prefix_excluded_all_four_tuples() -> None:
    """All four prefix filters hide bridge-internal rebar-type labels from user-facing labels."""
    from rebar.reconciler import (
        differ,
        inbound_collection_diffs,
        inbound_translate,
    )
    from rebar.reconciler import (
        outbound_labels as ol,
    )

    assert any("rebar-type:" == p for p in ol._EXCLUDED_PREFIXES)
    assert any("rebar-type:" == p for p in inbound_collection_diffs._EXCLUDED_PREFIXES)
    assert any("rebar-type:" == p for p in differ._BRIDGE_INTERNAL_LABEL_PREFIXES)
    assert any("rebar-type:" == p for p in inbound_translate._BRIDGE_INTERNAL_TAG_PREFIXES)


def test_jira_to_local_type_parity() -> None:
    """The reverse type map stays aligned with the inbound field mapper literal."""
    assert cfg_mod.jira_to_local_type == inbound_fields._JIRA_TO_LOCAL_TYPE


def test_local_type_vocab_derived_from_ticket_type() -> None:
    """Inbound recovery derives valid local types from TicketType so stamped types round-trip."""
    assert set(inbound_fields._LOCAL_TYPE_VOCAB) == set(get_args(TicketType))
    for local in get_args(TicketType):
        assert inbound_fields.recover_type_label([f"rebar-type:{local}"]) == local
