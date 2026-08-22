"""Per-project effective-map memoization within one outbound reconcile pass (d378).

Perf-only follow-up to the S2 advisory on 438a-e1d9-29d3-4081: inside
``compute_outbound_mutations`` the per-project effective config
(``config.effective_status_map`` / ``effective_type_map`` / ...) was resolved once per
TICKET, redoing the ``_discover_project_config`` filesystem stat-walk + three-layer
overlay resolution on every ticket. It must instead be memoized per ``project_key`` per
pass so the discovery runs once per DISTINCT project, not once per ticket — with
identical observable output.

Oracles (observable behaviour only — no wall-clock timing assert):

* PERF (call-count proxy): a spy over ``config.effective_status_map`` counts invocations.
  Over N unbound tickets spanning M distinct projects (M < N) it must be called exactly M
  times (once per distinct project per pass), not N. Pre-memoization it is called N times
  (RED-first); reverting the cache re-reddens it.
* PER-PROJECT KEYING: the spy records the ``project_key`` it was asked for; the set must be
  exactly the M distinct projects — a cache keyed WITHOUT ``project_key`` (one global slot)
  would collapse to a single call/key and fail this.
* OUTPUT UNCHANGED: with distinct per-project ``[mapping.projects.<KEY>.type_map]`` overlays
  the emitted create mutations must (a) be the full expected set (count + resolved
  ``_bridge_target_project`` stamp) and (b) each carry ITS OWN project's mapped
  ``issuetype`` — proving the memoized value returned per ticket is the right project's map.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rebar import config as user_cfg
from rebar_reconciler import binding_store as binding_store_mod
from rebar_reconciler import config as cfg_mod
from rebar_reconciler import outbound_differ as od
from rebar_reconciler import projects_store as ps

pytestmark = pytest.mark.unit

_RESERVED_TARGET_KEY = "_bridge_target_project"


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


def _proj(tmp: Path, mapping_toml: str) -> Path:
    """A repo root whose discovered ``rebar.toml`` carries ``mapping_toml`` verbatim."""
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    (p / "rebar.toml").write_text(mapping_toml, encoding="utf-8")
    user_cfg.reset_config_cache()
    return p


def _ticket(tid: str, project: str) -> dict[str, Any]:
    return {
        "ticket_id": tid,
        "title": "Some issue",
        "description": "desc",
        "status": "open",
        "priority": 2,
        "ticket_type": "task",
        "assignee": "",
        "tags": [],
        "comments": [],
        "deps": [],
        "bridge_project": project,
    }


# Two distinct projects, each overriding the ``task`` -> Jira type differently so the
# emitted ``issuetype`` observably identifies which project's map reached each ticket.
_MAPPING_TOML = (
    '[mapping.projects.A.type_map]\ntask = "Task"\n'
    '[mapping.projects.B.type_map]\ntask = "Deliverable"\n'
)


def _run(proj: Path, tmp_path: Path, tickets: list[dict[str, Any]]):
    store = binding_store_mod.BindingStore(tmp_path / ".tickets-tracker")  # empty -> all create
    mapping = ps.Mapping(
        legacy_default=None, projects={"A": {"repos": ["r"]}, "B": {"repos": ["s"]}}
    )
    return od.compute_outbound_mutations(
        local_tickets=tickets,
        jira_snapshot={},
        binding_store=store,
        config=od.OutboundDiffConfig(pass_id="p1", projects_mapping=mapping, repo_root=str(proj)),
    )


def test_effective_map_discovery_is_memoized_per_project_per_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = _proj(tmp_path, _MAPPING_TOML)
    # N = 6 unbound tickets spanning M = 2 distinct projects (M < N).
    tickets = [_ticket(f"a{i}", "A") for i in range(3)] + [_ticket(f"b{i}", "B") for i in range(3)]

    calls: list[str] = []
    _orig = cfg_mod.effective_status_map

    def _spy(project_key: str, root: object = None):
        calls.append(project_key)
        return _orig(project_key, root=root)

    monkeypatch.setattr(cfg_mod, "effective_status_map", _spy)

    mutations, _ = _run(proj, tmp_path, tickets)

    # PERF: discovery ran once per DISTINCT project (M=2), not once per ticket (N=6).
    assert len(calls) == 2, (
        f"config.effective_status_map must be memoized per project per pass: expected 2 "
        f"calls (one per distinct project) for 6 tickets across 2 projects, got {len(calls)}: "
        f"{calls}"
    )
    # PER-PROJECT KEYING: both distinct projects were resolved independently (a single
    # global cache slot would collapse this to one key).
    assert set(calls) == {"A", "B"}, calls

    # OUTPUT UNCHANGED: the full create set, each stamped with its resolved project and
    # carrying ITS OWN project's mapped issuetype (proves the right map reached each ticket).
    creates = [m for m in mutations if getattr(m, "action", "") == "create"]
    assert len(creates) == 6, [(m.local_id, m.action) for m in mutations]
    by_id = {m.local_id: m for m in creates}
    for i in range(3):
        a = by_id[f"a{i}"]
        assert a.fields.get(_RESERVED_TARGET_KEY) == "A"
        assert a.fields.get("issuetype") == "Task", a.fields
        b = by_id[f"b{i}"]
        assert b.fields.get(_RESERVED_TARGET_KEY) == "B"
        assert b.fields.get("issuetype") == "Deliverable", b.fields
