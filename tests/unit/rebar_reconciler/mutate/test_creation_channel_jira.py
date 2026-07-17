"""creation_channel for the Jira reconciler (story e622, epic jira-reb-977).

The inbound Jira materialization path assembles CREATE data directly (bypassing
composer.create_core), so it must stamp a validated ``creation_channel="jira"`` —
on the ticket AND on any placeholder identity it mints. Outbound bind/push must
never rewrite genesis provenance. And a legacy full-log Jira CREATE (no recorded
channel) is inferred to ``jira`` ONLY under the exact three-signal predicate
(``jira-*`` id + author == env_id == "reconciler"); every near miss projects
``unknown`` with no inference marker.

Observable oracle only: persisted CREATE.data + reduced ticket state. Reconciler
modules are spec-loaded by path (hyphenated engine dir can't be normal-imported),
mirroring test_inbound_leaf_bodies.py.

``-k`` selectors: inbound_recorded, outbound, legacy_positive, legacy_negative.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import rebar
from rebar.reducer import reduce_ticket

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
MUTATION_PATH = APPLIER_PATH.parent / "mutation.py"


def _load_mutation_module():
    canonical = "rebar_reconciler.mutation"
    if canonical in sys.modules:
        return sys.modules[canonical]
    spec = importlib.util.spec_from_file_location(canonical, MUTATION_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[canonical] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier_under_test", APPLIER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mut_mod():
    return _load_mutation_module()


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


@pytest.fixture
def fixture_repo(tmp_path, monkeypatch):
    """A fully-initialised rebar repo (so library calls + inbound identity minting
    work), with REBAR_ENV_ID/AUTHOR unset so reconciler inbound events get the
    "reconciler" defaults (the legacy Jira signature)."""
    import subprocess

    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.delenv("REBAR_ENV_ID", raising=False)
    monkeypatch.delenv("REBAR_AUTHOR", raising=False)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=tmp_path, check=True, capture_output=True
    )
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    rebar.init_repo(repo_root=str(tmp_path))
    return tmp_path


def _make_mutation(mut_mod, *, direction, action, target, payload=None, provenance=None):
    return mut_mod.Mutation(
        direction=direction,
        action=action,
        target=target,
        payload=payload or {},
        provenance=provenance or {"source": "test"},
    )


def _read_create(tracker_dir: Path, local_id: str) -> dict:
    for path in sorted((tracker_dir / local_id).glob("*.json")):
        ev = json.loads(path.read_text())
        if ev.get("event_type") == "CREATE":
            return ev
    raise AssertionError(f"no CREATE event in {tracker_dir / local_id}")


def _inbound_create(applier, mut_mod, fixture_repo, *, assignee=None):
    fields = {"summary": "X", "priority": 2, "status": "To Do", "issuetype": "Task"}
    if assignee is not None:
        fields["assignee"] = assignee
    mutation = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-123",
        payload={"fields": fields, "labels": []},
    )
    applier._apply_typed(mutation, repo_root=fixture_repo)
    return fixture_repo / ".tickets-tracker", "jira-dig-123"


def _write_legacy_create(tmp_path: Path, *, ticket_id: str, author, env_id) -> dict:
    """Build a channel-less legacy CREATE with the given author/env_id and reduce it."""
    tdir = tmp_path / ticket_id
    tdir.mkdir(parents=True)
    event = {
        "event_type": "CREATE",
        "uuid": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
        "timestamp": 1700000000,
        "author": author,
        "env_id": env_id,
        "data": {"id": ticket_id, "ticket_type": "task", "title": "legacy", "priority": 2},
    }
    if author is None:
        del event["author"]
    if env_id is None:
        del event["env_id"]
    (tdir / "1700000000-aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa-CREATE.json").write_text(
        json.dumps(event)
    )
    return reduce_ticket(str(tdir))


# ── inbound_recorded (AC1): the direct Jira writer stamps a validated `jira` ──
def test_inbound_recorded_jira_create(applier, mut_mod, fixture_repo):
    tracker, local_id = _inbound_create(applier, mut_mod, fixture_repo)
    data = _read_create(tracker, local_id)["data"]
    assert data["creation_channel"] == "jira"
    # Recorded, not inferred — no marker on a recorded value.
    assert "creation_channel_inferred" not in data
    state = reduce_ticket(str(tracker / local_id))
    assert state["creation_channel"] == "jira"
    assert "creation_channel_inferred" not in state


def test_inbound_recorded_placeholder_identity_is_jira(applier, mut_mod, fixture_repo):
    assignee = {"accountId": "acct-xyz-789", "displayName": "Jira User"}
    tracker, _ = _inbound_create(applier, mut_mod, fixture_repo, assignee=assignee)
    idents = [
        t
        for t in rebar.list_tickets(ticket_type="identity", repo_root=str(fixture_repo))
        if t.get("creation_channel")
    ]
    assert idents, "expected a placeholder identity minted for the inbound assignee"
    assert all(t["creation_channel"] == "jira" for t in idents), [
        (t["ticket_id"], t.get("creation_channel")) for t in idents
    ]


# ── outbound (AC2): binding/pushing never rewrites genesis provenance ─────────
def test_outbound_sync_edit_preserves_creation_channel(fixture_repo):
    # Outbound bind/push writes no CREATE and, when a Jira sync writes back an
    # EDIT/STATUS, genesis provenance must survive it (a regression guard: the
    # local channel of a python ticket is never rewritten by later mutation).
    tid = rebar.create_ticket("task", "local work", repo_root=str(fixture_repo))
    tracker = fixture_repo / ".tickets-tracker"
    assert _read_create(tracker, tid)["data"]["creation_channel"] == "python"
    # A Jira-driven writeback edit + status change (the shapes an outbound sync uses).
    rebar.edit_ticket(tid, description="synced from jira", repo_root=str(fixture_repo))
    rebar.claim(tid, assignee="me", repo_root=str(fixture_repo))
    assert reduce_ticket(str(tracker / tid))["creation_channel"] == "python"


# ── legacy_positive (AC5): all three signals match -> jira + inferred marker ──
def test_legacy_positive_infers_jira_with_marker(tmp_path):
    state = _write_legacy_create(
        tmp_path, ticket_id="jira-dig-999", author="reconciler", env_id="reconciler"
    )
    assert state["creation_channel"] == "jira"
    assert state["creation_channel_inferred"] is True


# ── legacy_negative (AC6): break any one signal -> unknown, no marker ─────────
@pytest.mark.parametrize(
    "ticket_id,author,env_id,why",
    [
        ("local-dig-999", "reconciler", "reconciler", "non-jira id"),
        ("jira-dig-999", "someone-else", "reconciler", "wrong author"),
        ("jira-dig-999", "reconciler", "prod-env", "wrong env_id"),
        ("jira-dig-999", None, "reconciler", "missing author"),
        ("jira-dig-999", "reconciler", None, "missing env_id"),
    ],
)
def test_legacy_negative_near_miss_is_unknown(tmp_path, ticket_id, author, env_id, why):
    state = _write_legacy_create(tmp_path, ticket_id=ticket_id, author=author, env_id=env_id)
    assert state["creation_channel"] == "unknown", why
    assert "creation_channel_inferred" not in state, why
