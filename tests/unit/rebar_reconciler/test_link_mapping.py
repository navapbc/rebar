"""Config-driven link-relation mapping — the S4 outbound seam, skip-not-approximate
preserved (epic ravenous-dirt-widgeon / 1515-bebc-0d91-476b).

One effective ``link_map`` governs BOTH halves of link emission — the ADD pass
(``outbound_links._diff_links``) and the managed-ref-gated REMOVE pass
(``outbound_links._diff_link_removals``) — through a single ``_resolve_link`` seam:

  * an override replaces the built-in Jira link TYPE while preserving the built-in
    direction ``swap``;
  * a ``skip`` override (and the built-in unsynced relations) suppress BOTH ADD and
    REMOVE — never added, never removed, never approximated;
  * REMOVE attribution is config-aware: a remote link carrying an OVERRIDDEN vendor
    type — which the built-in reverse map (``link_direction.JIRA_LINK_TO_RELATION``)
    does not know, so ``map_remote_links`` hands back ``relation=None`` — is recovered
    via the inverted ``link_map`` rather than silently dropped;
  * ``config.effective_link_map`` fails closed (``MappingConfigError``) on a link_map
    value outside a declared ``link_types`` vocabulary.

Assertions target OBSERVABLE behaviour only — the resolved map, and the vendor-shaped
ADD/REMOVE mutation dicts the diff funcs emit — never private structure. Built-in
behaviour (no ``[mapping]`` block) must stay bit-for-bit what it is today.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from rebar import config as user_cfg
from rebar_reconciler import config as cfg_mod
from rebar_reconciler.adapters.jira_family.value_maps import RELATION_TO_JIRA_LINK
from rebar_reconciler.mapping_config import SKIP, MappingConfigError

pytestmark = pytest.mark.unit

_REC = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _REC / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _BS:
    """A minimal bidirectional binding store (local_id <-> Jira key)."""

    def __init__(self, l2j: dict[str, str]) -> None:
        self.l2j = l2j
        self.j2l = {v: k for k, v in l2j.items()}

    def get_jira_key(self, local_id: str) -> str | None:
        return self.l2j.get(local_id)

    def get_local_id(self, jira_key: str) -> str | None:
        return self.j2l.get(jira_key)


def _dep(target: str, relation: str, uuid: str = "u1") -> dict[str, object]:
    return {"target_id": target, "relation": relation, "link_uuid": uuid}


def _link(type_name: str, inward: str | None = None, outward: str | None = None) -> dict:
    d: dict[str, object] = {"type": {"name": type_name}}
    if inward:
        d["inwardIssue"] = {"key": inward}
    if outward:
        d["outwardIssue"] = {"key": outward}
    return d


def _managed_ticket(relation: str, target: str) -> dict[str, object]:
    """A ticket whose ``(relation, target)`` link is MANAGED but locally absent — the
    shape that makes a REMOVE eligible to propagate."""
    return {"ticket_id": "loc-1", "deps": [], "managed_refs": [[relation, target]]}


@pytest.fixture(scope="module")
def backend():
    from rebar_reconciler.adapters.jira.backend import JiraBackend

    return JiraBackend(transport=object())


@pytest.fixture(scope="module")
def ol() -> ModuleType:
    return _load("outbound_links_s4", "outbound_links.py")


# ---------------------------------------------------------------------------
# effective_link_map config discovery isolation
# ---------------------------------------------------------------------------


@pytest.fixture()
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xdg = tmp_path / "xdg"
    (xdg / "rebar").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    for name in ("REBAR_CONFIG", "REBAR_ROOT", "REBAR_CONFIG_UNKNOWN_KEYS"):
        monkeypatch.delenv(name, raising=False)
    user_cfg.set_cli_overrides(None)
    user_cfg.reset_config_cache()


def _proj(tmp: Path, mapping_toml: str = "", *, name: str = "proj") -> Path:
    p = tmp / name
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    (p / "rebar.toml").write_text(mapping_toml, encoding="utf-8")
    user_cfg.reset_config_cache()
    return p


# ===========================================================================
# VISIBLE happy-path oracle
# ===========================================================================


def test_effective_link_map_default_equals_builtin(_isolated_config, tmp_path: Path) -> None:
    """With NO ``[mapping]`` block the effective link map equals the built-in
    ``config.local_to_jira_link`` verbatim — the seam is inert until configured."""
    proj = _proj(tmp_path)
    assert cfg_mod.effective_link_map("REB", proj) == dict(cfg_mod.local_to_jira_link)


def test_link_override_changes_add_type(ol, backend) -> None:
    """A ``link_map`` override of ``relates_to`` changes the ADD mutation's vendor
    ``type`` while preserving the built-in ``swap`` (False for relates_to)."""
    bs = _BS({"loc-1": "DIG-1", "tgt": "DIG-9"})
    ticket = {"ticket_id": "loc-1", "deps": [_dep("tgt", "relates_to")]}
    out = ol._diff_links(
        ticket, {"issuelinks": []}, bs, backend, link_map={"relates_to": "Relationship"}
    )
    assert out == [
        {
            "action": "add",
            "type": "Relationship",
            "to_key": "DIG-9",
            "relation": "relates_to",
            "swap": False,
            "link_uuid": "u1",
        }
    ]


def test_no_link_map_add_unchanged(ol, backend) -> None:
    """No ``link_map`` (None) leaves the ADD byte-for-byte the built-in payload."""
    bs = _BS({"loc-1": "DIG-1", "tgt": "DIG-9"})
    ticket = {"ticket_id": "loc-1", "deps": [_dep("tgt", "blocks")]}
    out = ol._diff_links(ticket, {"issuelinks": []}, bs, backend, link_map=None)
    assert out == [
        {
            "action": "add",
            "type": "Blocks",
            "to_key": "DIG-9",
            "relation": "blocks",
            "swap": False,
            "link_uuid": "u1",
        }
    ]


def test_local_to_jira_link_parity() -> None:
    """``config.local_to_jira_link`` is kept in lock-step with the link-TYPE component
    of the adapter's ``RELATION_TO_JIRA_LINK`` (the parity guard)."""
    assert cfg_mod.local_to_jira_link == {
        rel: link_type for rel, (link_type, _swap) in RELATION_TO_JIRA_LINK.items()
    }


# ===========================================================================
# HELD-OUT edge / contract oracle
# ===========================================================================


def test_remove_config_aware_attribution(ol, backend) -> None:
    """A managed, locally-absent remote link carrying an OVERRIDDEN vendor type
    ("Relationship") — which the built-in reverse map does not know — is attributed via
    the inverted ``link_map`` (not dropped) and emits a REMOVE targeting that same
    overridden type."""
    bs = _BS({"loc-1": "DIG-1", "tgt": "DIG-9"})
    ticket = _managed_ticket("relates_to", "tgt")
    jira = {"issuelinks": [_link("Relationship", outward="DIG-9")]}
    out = ol._diff_link_removals(ticket, jira, bs, backend, link_map={"relates_to": "Relationship"})
    assert out == [
        {"action": "remove", "type": "Relationship", "to_key": "DIG-9", "relation": "relates_to"}
    ]


def test_link_skip_suppresses_add(ol, backend) -> None:
    """A ``skip`` override suppresses the ADD entirely — no approximate link is
    substituted."""
    bs = _BS({"loc-1": "DIG-1", "tgt": "DIG-9"})
    ticket = {"ticket_id": "loc-1", "deps": [_dep("tgt", "relates_to")]}
    out = ol._diff_links(ticket, {"issuelinks": []}, bs, backend, link_map={"relates_to": SKIP})
    assert out == []


def test_link_skip_suppresses_remove(ol, backend) -> None:
    """A ``skip`` override on a relation ALSO suppresses its REMOVE: a skipped relation
    is neither added nor removed, even when the remote still carries the built-in link
    type. Symmetry keeps the inbound differ from re-adding it each pass."""
    bs = _BS({"loc-1": "DIG-1", "tgt": "DIG-9"})
    ticket = _managed_ticket("relates_to", "tgt")
    jira = {"issuelinks": [_link("Relates", outward="DIG-9")]}
    out = ol._diff_link_removals(ticket, jira, bs, backend, link_map={"relates_to": SKIP})
    assert out == []


def test_override_preserves_builtin_swap_for_depends_on(ol, backend) -> None:
    """An override of ``depends_on`` changes only the link TYPE; the built-in direction
    ``swap`` (True for depends_on) is preserved."""
    bs = _BS({"loc-1": "DIG-1", "tgt": "DIG-9"})
    ticket = {"ticket_id": "loc-1", "deps": [_dep("tgt", "depends_on")]}
    out = ol._diff_links(
        ticket, {"issuelinks": []}, bs, backend, link_map={"depends_on": "Dependency"}
    )
    assert out == [
        {
            "action": "add",
            "type": "Dependency",
            "to_key": "DIG-9",
            "relation": "depends_on",
            "swap": True,
            "link_uuid": "u1",
        }
    ]


def test_unmapped_remote_type_still_adopted(ol, backend) -> None:
    """A remote link whose vendor type is unknown to BOTH the built-in reverse map and
    the ``link_map`` stays ``relation=None`` and is left for inbound ADOPT — never
    spuriously removed, even for a managed ticket."""
    bs = _BS({"loc-1": "DIG-1", "tgt": "DIG-9"})
    ticket = _managed_ticket("relates_to", "tgt")
    jira = {"issuelinks": [_link("Mentions", outward="DIG-9")]}
    out = ol._diff_link_removals(ticket, jira, bs, backend, link_map={"relates_to": "Relationship"})
    assert out == []


def test_link_undeclared_fails_closed(_isolated_config, tmp_path: Path) -> None:
    """A ``link_map`` value outside a declared ``link_types`` vocabulary makes
    ``effective_link_map`` fail closed with ``MappingConfigError``."""
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB]\nlink_types = ["Blocks", "Relates"]\n'
        '[mapping.projects.REB.link_map]\nrelates_to = "Relationship"\n',
    )
    with pytest.raises(MappingConfigError):
        cfg_mod.effective_link_map("REB", proj)
