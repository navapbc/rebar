"""Frozen-artifact census replay (epic 3006-e198 derisk AC).

Replays the pure classifier over the sanitized 2026-07-03 production artifacts
(``tests/fixtures/bridge_convergence/frozen-2026-07-03/``: bindings + snapshot +
local states, PII/bodies stripped) and asserts EXACTLY the 7 known drift
decisions and nothing else:

  * 5× TERMINAL_TRANSITION — REB-464 / REB-456 / REB-465 / REB-466 / REB-457
  * 1× PROBE_GET          — REB-530 (f8b5-2f30, Jira issue deleted, out of window)
  * 1× ADOPT              — REB-532 (Jira-native, unbound)

then asserts a fixed point (zero drift) after a simulated heal. This is the
regression cell that would have caught drift classes A/B/C before they shipped.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.rebar_reconciler.classify._load import load_classify

c = load_classify()
ObservedJira = c.ObservedJira
DecisionKind = c.DecisionKind
JiraObservation = c.JiraObservation

_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "fixtures"
    / "bridge_convergence"
    / "frozen-2026-07-03"
)


def _load_fixture():
    store = json.loads((_FIXTURE / "bindings.json").read_text())
    snapshot = json.loads((_FIXTURE / "snapshot.json").read_text())
    locals_ = json.loads((_FIXTURE / "locals.json").read_text())
    return store, snapshot, locals_


def _replay(store, snapshot, locals_, retired: set[str] | None = None) -> dict:
    """Run the classifier over every cell, exactly as the live/audit consumers do.

    Returns ``{jira_key: Decision}``.
    """
    retired = retired or set()
    decisions: dict = {}
    bound_keys = set(store["reverse"])
    # 1) binding-store walk (every bound pair).
    for local_id, entry in store["bindings"].items():
        jira_key = entry["jira_key"]
        local = locals_.get(local_id)
        if jira_key in snapshot:
            obs = JiraObservation(ObservedJira.PRESENT, key=jira_key, fields=snapshot[jira_key])
        else:
            obs = JiraObservation(ObservedJira.ABSENT_IN_WINDOW, key=jira_key)
        decisions[jira_key] = c.classify(local, obs, entry, entry.get("baseline"))
    # 2) fetched-unbound walk (adoption).
    for jira_key, fields in snapshot.items():
        if jira_key not in bound_keys:
            obs = JiraObservation(
                ObservedJira.PRESENT, key=jira_key, fields=fields, retired=jira_key in retired
            )
            decisions[jira_key] = c.classify(None, obs, None, None)
    return decisions


def test_frozen_census_is_exactly_the_seven_known_drifts():
    store, snapshot, locals_ = _load_fixture()
    decisions = _replay(store, snapshot, locals_)

    counts: dict = {}
    for d in decisions.values():
        counts[d.kind] = counts.get(d.kind, 0) + 1

    # The 2026-07-03 spike shape: 608 sync + 5 terminal + 1 probe + 1 adopt = 615.
    assert counts.get(DecisionKind.SYNC_FIELDS) == 608
    assert counts.get(DecisionKind.TERMINAL_TRANSITION) == 5
    assert counts.get(DecisionKind.PROBE_GET) == 1
    assert counts.get(DecisionKind.ADOPT) == 1
    assert sum(counts.values()) == 615

    terminal_keys = {k for k, d in decisions.items() if d.kind is DecisionKind.TERMINAL_TRANSITION}
    assert terminal_keys == {"REB-464", "REB-456", "REB-465", "REB-466", "REB-457"}
    probe_keys = {k for k, d in decisions.items() if d.kind is DecisionKind.PROBE_GET}
    assert probe_keys == {"REB-530"}
    adopt_keys = {k for k, d in decisions.items() if d.kind is DecisionKind.ADOPT}
    assert adopt_keys == {"REB-532"}


def test_frozen_census_matches_census_helper():
    store, snapshot, locals_ = _load_fixture()
    decisions = list(_replay(store, snapshot, locals_).values())
    rec = c.census(decisions, total_bindings=len(store["bindings"]), max_acting_fraction=0.10)
    # 5 terminal + 1 adopt are ACTING (PROBE_GET is a bounded read, not acting).
    assert rec["acting_count"] == 6
    assert rec["counts"]["terminal_transition"] == 5
    assert rec["counts"]["adopt"] == 1
    assert rec["counts"]["probe_get"] == 1
    # 6 / 614 ≈ 0.98% — well under the 10% breaker (8.8× headroom).
    assert rec["breaker"]["allowed"] is True
    assert rec["acting_pct"] < 1.5


def test_fixed_point_after_simulated_heal():
    store, snapshot, locals_ = _load_fixture()

    # Simulate the heal:
    #  A) the 5 archived-terminal Jira issues transition To Do → Done.
    for key in ("REB-464", "REB-456", "REB-465", "REB-466", "REB-457"):
        snapshot[key] = {**snapshot[key], "status": "Done"}
    #  C) REB-530's dangling binding is retired (removed from the store).
    lid_530 = store["reverse"].pop("REB-530")
    store["bindings"].pop(lid_530)
    #  B) REB-532 is adopted: create local + binding + baseline (echo-suppressed).
    store["bindings"]["jira-532-adopted"] = {
        "jira_key": "REB-532",
        "state": "confirmed",
        "baseline": dict(snapshot["REB-532"]),
    }
    store["reverse"]["REB-532"] = "jira-532-adopted"
    locals_["jira-532-adopted"] = {
        "ticket_id": "jira-532-adopted",
        "status": "open",
        "archived": False,
    }

    decisions = _replay(store, snapshot, locals_)
    acting = {k: d.kind for k, d in decisions.items() if d.is_acting}
    assert acting == {}, f"not a fixed point after heal: {acting}"
    # REB-530 no longer produces any cell (unbound + not fetched).
    assert "REB-530" not in decisions
