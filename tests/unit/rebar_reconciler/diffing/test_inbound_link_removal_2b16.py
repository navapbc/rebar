"""An issue link DELETED on the peer must be mirrored onto the local ticket (ticket 2b16).

`inbound_differ._diff_links_inbound` was ADD-ONLY by construction — its docstring opened
"Reflect Jira issuelinks into rebar relations. ADD-only." and closed "ADD-only (no REMOVE
mutations)". It iterates the CURRENT `fields.issuelinks` and emits an add per unseen link;
nothing walked the local `deps` looking for one whose peer counterpart had gone. So a link a
human deletes in Jira stayed on the rebar ticket forever and no pass ever reported it.

WHY THIS MODULE IS SHAPED THE WAY IT IS
---------------------------------------
An inbound removal is a WRITE THAT DESTROYS LOCAL DATA, so the interesting assertions here are
the ones that prove a removal is NOT emitted. `fetcher.py:588-590` states the pre-2b16 safety
argument outright — "on any failure the enrichment is skipped (differs degrade to 'no Jira
links' — additive ADD-only sync stays safe)" — and this change VOIDS it: the moment a REMOVE
path exists, a truncated or failed enrichment makes every issue look link-less. This epic has
already fixed three silent-truncation defects in this exact data path, all fail-open and all
silent. The guard oracles (G1..G5) are therefore the ones that protect the product; the
happy-path oracle alone is satisfied by an unconditional removal, which is a data-loss bug.

Every oracle drives PRODUCTION ENTRY POINTS — `compute_inbound_mutations` and
`_inbound_update_apply_links` — never a private helper by name. That is deliberate: it is what
makes the pre-fix run report "no removal was emitted" rather than an `AttributeError` against a
function that does not exist yet, and it is what makes the end-to-end cell traverse the layer
(`apply_inbound_records.py:560-562`) where the record used to be silently discarded.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakeBindingStore:
    """The two lookups the link differ + its bidir suppression use."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._by_key = dict(mapping)
        self._by_local = {v: k for k, v in mapping.items()}

    def get_local_id(self, jira_key: str) -> str | None:
        return self._by_key.get(jira_key)

    def get_jira_key(self, local_id: str) -> str | None:
        return self._by_local.get(local_id)


class _StubMapper:
    """An `InboundMapper` that maps nothing, so only the LINK diff is under test."""

    def map_remote_to_local(self, remote_fields: dict[str, Any]) -> dict[str, Any]:
        return {}


class _FakeOutbound:
    """The two attributes `_build_outbound_context` reads off an outbound mutation."""

    def __init__(self, jira_key: str, links: list[dict[str, Any]]) -> None:
        self.jira_key = jira_key
        self.links = links
        self.labels: list[dict[str, Any]] = []
        self.fields: dict[str, Any] = {}


def _blocks_outward(other_key: str) -> dict[str, Any]:
    """The issuelink Jira shows on X when X BLOCKS ``other_key`` (outward side)."""
    return {
        "id": "10001",
        "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
        "outwardIssue": {"key": other_key},
    }


def _blocks_inward(other_key: str) -> dict[str, Any]:
    """The MIRROR entry Jira shows on the far end of the same link (inward side)."""
    return {
        "id": "10001",
        "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
        "inwardIssue": {"key": other_key},
    }


def _local(ticket_id: str, deps: list[tuple[str, str]], managed: list[tuple[str, str]]) -> dict:
    """A reduced local ticket carrying ``deps`` and the ``managed_refs`` projection."""
    return {
        "ticket_id": ticket_id,
        "deps": [
            {"relation": rel, "target_id": tgt, "link_uuid": f"uuid-{rel}-{tgt}"}
            for rel, tgt in deps
        ],
        "managed_refs": [[kind, target] for kind, target in managed],
    }


def _enrich_via_fetcher(
    snapshot: dict[str, dict[str, Any]], issuelinks_map: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Merge an issuelinks map into a snapshot via the PRODUCTION merge.

    Drives ``fetcher.merge_issuelinks_map`` — the named seam extracted by ticket 6c0a —
    rather than the byte-faithful hand copy (``_enrich_via_fetcher``) this replaces. The
    point is the CONDITIONAL inside that seam: an issue the map does not mention never gets
    the key at all. That is the real truncation/fail-open shape — a partial map from a broken
    page walk, an HTTP 410, a cross-project issue, or a client with no
    ``get_issuelinks_map`` — so G1's unobserved case is produced HERE by omitting the issue
    from the map, not by hand-deleting a key off a finished entry. An issue the map DOES
    mention with ``[]`` gets an authoritative empty list, which is the
    observed-and-genuinely-link-less case that MUST be able to remove. Because this calls
    the production merge, any drift in that rule turns these tests RED instead of leaving a
    stale copy green.
    """
    fetcher = importlib.import_module("rebar_reconciler.fetcher")
    return fetcher.merge_issuelinks_map(snapshot, issuelinks_map)


def _inbound(
    snapshot: dict[str, dict[str, Any]],
    bindings: _FakeBindingStore,
    locals_by_id: dict[str, dict[str, Any]],
    outbound: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Run the production inbound differ and return the flat list of link records."""
    inbound_differ = importlib.import_module("rebar_reconciler.inbound_differ")
    muts, _suppressed = inbound_differ.compute_inbound_mutations(
        snapshot,
        bindings,
        locals_by_id,
        outbound,
        inbound_mapper=_StubMapper(),
    )
    out: list[dict[str, Any]] = []
    for m in muts:
        out.extend(getattr(m, "links", []) or [])
    return out


def _removals(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r.get("action") == "remove"]


# A managed `blocks` dep from local-a to local-b, both bound, nothing else going on.
def _one_managed_dep() -> tuple[_FakeBindingStore, dict[str, dict[str, Any]]]:
    bindings = _FakeBindingStore({"DC-1": "local-a", "DC-2": "local-b"})
    local_a = _local("local-a", [("blocks", "local-b")], [("blocks", "local-b")])
    local_b = _local("local-b", [], [])
    return bindings, {"local-a": local_a, "local-b": local_b}


# ---------------------------------------------------------------------------
# The bug: a managed dep whose peer link is gone must be removed
# ---------------------------------------------------------------------------


def test_peer_deleted_link_emits_a_removal() -> None:
    """THE DEFECT. `DC-1` is observed with zero links; the managed dep to `local-b` must go.

    Pre-fix this emits nothing at all — the differ never walks the local deps — so the RED
    message reads "no removal was emitted", which names the missing removal rather than an
    absent symbol.
    """
    bindings, locals_by_id = _one_managed_dep()
    snapshot = _enrich_via_fetcher({"DC-1": {}}, {"DC-1": []})

    records = _inbound(snapshot, bindings, locals_by_id)

    assert _removals(records) == [
        {"action": "remove", "target_id": "local-b", "relation": "blocks"}
    ], (
        "a link deleted on the peer was NOT mirrored: no removal was emitted for the managed "
        f"dep blocks->local-b. records={json.dumps(records)}"
    )


def test_g1_observed_but_empty_issuelinks_still_removes() -> None:
    """G1 positive half: `issuelinks: []` is an AUTHORITATIVE "no links", so it must remove.

    This is the case a naive `jira_fields.get("issuelinks") or []` guard cannot distinguish
    from "we never looked", which is why the removal path must test key PRESENCE. If this cell
    is red while `test_peer_deleted_link_emits_a_removal` is green, the guard was written as a
    truthiness check and the feature is dead for every genuinely link-less issue.
    """
    bindings, locals_by_id = _one_managed_dep()
    snapshot = _enrich_via_fetcher({"DC-1": {}}, {"DC-1": []})

    assert "issuelinks" in snapshot["DC-1"], "SETUP FAILED: the fetcher merge did not set the key"
    assert snapshot["DC-1"]["issuelinks"] == [], "SETUP FAILED: expected an authoritative []"

    assert len(_removals(_inbound(snapshot, bindings, locals_by_id))) == 1, (
        "an observed-and-empty issuelinks list did not produce a removal; the guard is "
        "probably a truthiness check on the value instead of a presence check on the key"
    )


# ---------------------------------------------------------------------------
# G1 — never infer a deletion from data we did not observe
# ---------------------------------------------------------------------------


def test_g1_unobserved_issuelinks_emits_no_removal() -> None:
    """G1: the issue is ABSENT from the issuelinks map, so its links were never observed.

    Produced through the production merge rule with a TRUNCATED map (see
    `_enrich_via_fetcher`) — the shape a fail-open partial page walk really leaves behind.
    Trusting this absence would convert the three already-fixed silent READ truncations in this
    path into a WRITE that deletes every managed dep in the project.
    """
    bindings, locals_by_id = _one_managed_dep()
    # The map came back WITHOUT DC-1 (truncated page / failed enrichment).
    snapshot = _enrich_via_fetcher({"DC-1": {}}, {"DC-9": []})

    assert "issuelinks" not in snapshot["DC-1"], (
        "SETUP FAILED: the truncated map must leave the key absent, or this cell proves nothing"
    )

    records = _inbound(snapshot, bindings, locals_by_id)
    assert _removals(records) == [], (
        "a removal was inferred from UNOBSERVED link data — a truncated or failed enrichment "
        f"would delete real local deps. records={json.dumps(records)}"
    )


def test_g1_non_list_issuelinks_emits_no_removal() -> None:
    """G1 edge: a malformed (non-list) value is not an observation either."""
    bindings, locals_by_id = _one_managed_dep()
    snapshot = {"DC-1": {"issuelinks": "not-a-list"}}

    assert _removals(_inbound(snapshot, bindings, locals_by_id)) == [], (
        "a malformed issuelinks value was treated as an authoritative empty link set"
    )


# ---------------------------------------------------------------------------
# The merge seam itself — the key-presence contract G1 depends on (ticket 6c0a)
# ---------------------------------------------------------------------------
#
# These cells drive ``fetcher.merge_issuelinks_map`` DIRECTLY. They pin the
# characterization taken from the inline merge BEFORE the 6c0a extraction, so a
# behaviour change in the extraction (or any later edit to the seam) turns them
# RED here rather than silently disarming G1 above.


def test_merge_seam_observed_vs_unobserved_key_presence() -> None:
    """The property G1 depends on: key PRESENT means observed, key ABSENT means not.

    An issue the map mentions with zero links gets ``"issuelinks": []`` (authoritative,
    removal may fire); an issue the map never mentioned gets NO key at all (unobserved,
    removal must not fire). Collapsing these two states is the exact drift that would let
    the removal path delete local deps from a truncated read.
    """
    snapshot = _enrich_via_fetcher({"DC-1": {}, "DC-2": {}}, {"DC-1": []})

    assert snapshot["DC-1"] == {"issuelinks": []}, (
        "an issue OBSERVED with zero links must carry the issuelinks key with an empty "
        f"list — got {snapshot['DC-1']!r}"
    )
    assert "issuelinks" not in snapshot["DC-2"], (
        "MUTATION TRIPWIRE: an issue the map never mentioned gained an issuelinks key — "
        "key-presence no longer means 'observed', which disarms G1 and lets a truncated "
        f"read delete local deps. entry={snapshot['DC-2']!r}"
    )


def test_merge_seam_characterization_pinned_before_extraction() -> None:
    """Characterization of the inline merge, captured BEFORE the 6c0a extraction.

    Each row was produced by exec-ing the then-inline production lines
    (`fetcher.py:629-633` at commit 75cdfec) against the input; the extracted seam must
    reproduce them byte-for-byte — the extraction's zero-behaviour-change contract.
    """
    cases: list[tuple[str, dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]] = [
        (
            "observed links",
            {"DC-1": {}},
            {"DC-1": [{"id": "1"}]},
            {"DC-1": {"issuelinks": [{"id": "1"}]}},
        ),
        ("map key not in snapshot is ignored", {"DC-1": {}}, {"DC-2": [{"id": "1"}]}, {"DC-1": {}}),
        ("non-list value is not an observation", {"DC-1": {}}, {"DC-1": "garbage"}, {"DC-1": {}}),
        ("None value is not an observation", {"DC-1": {}}, {"DC-1": None}, {"DC-1": {}}),
        (
            "a fresher list overwrites an existing key",
            {"DC-1": {"issuelinks": [1]}},
            {"DC-1": [2]},
            {"DC-1": {"issuelinks": [2]}},
        ),
    ]
    for name, snapshot, issuelinks_map, expected in cases:
        result = _enrich_via_fetcher(snapshot, issuelinks_map)
        assert result == expected, (
            f"characterization case {name!r} diverged from the pre-extraction inline merge: "
            f"got {result!r}, pinned {expected!r}"
        )
        assert result is snapshot, "the seam must mutate and return the SAME snapshot dict"


# ---------------------------------------------------------------------------
# G2 — an unbound target proves nothing
# ---------------------------------------------------------------------------


def test_g2_unbound_target_emits_no_removal() -> None:
    """G2: `local-b` has no peer binding, so a peer link to it could never have existed.

    Its absence from the peer's link set is therefore not evidence of a deletion.
    """
    bindings = _FakeBindingStore({"DC-1": "local-a"})  # local-b deliberately unbound
    local_a = _local("local-a", [("blocks", "local-b")], [("blocks", "local-b")])
    snapshot = _enrich_via_fetcher({"DC-1": {}}, {"DC-1": []})

    records = _inbound(snapshot, bindings, {"local-a": local_a})
    assert _removals(records) == [], (
        "a dep with an UNBOUND target was removed; the peer never could have carried that "
        f"link, so its absence is not evidence. records={json.dumps(records)}"
    )


# ---------------------------------------------------------------------------
# G3 — never clobber a ref rebar does not own
# ---------------------------------------------------------------------------


def test_g3_unmanaged_dep_emits_no_removal() -> None:
    """G3: a dep absent from `managed_refs` is not ours to delete.

    `managed_refs` is the provider-agnostic gate the outbound parent/link paths already
    consume. An empty projection means "nothing managed", under which the gate returns False
    for every ref — fail-open, so a transient or migrated-away projection can only delay
    convergence, never fire an irreversible wrong removal.
    """
    bindings = _FakeBindingStore({"DC-1": "local-a", "DC-2": "local-b"})
    local_a = _local("local-a", [("blocks", "local-b")], [])  # dep present, NOT managed
    local_b = _local("local-b", [], [])
    snapshot = _enrich_via_fetcher({"DC-1": {}}, {"DC-1": []})

    records = _inbound(snapshot, bindings, {"local-a": local_a, "local-b": local_b})
    assert _removals(records) == [], (
        "an UNMANAGED dep was removed — a peer-created or not-yet-pushed local ref must never "
        f"be clobbered. records={json.dumps(records)}"
    )


@pytest.mark.parametrize("relation", ["duplicates", "supersedes", "discovered_from"])
def test_g3_relations_with_no_peer_link_type_are_never_removed(relation: str) -> None:
    """G3, for free: relations with no reliable peer link type are outside `MANAGED_REF_KINDS`.

    `duplicates` / `supersedes` / `discovered_from` are never synced, so they can never be
    "managed", so the managed gate excludes them with no dedicated filter. Pinned because a
    future contributor adding a redundant explicit filter (or widening the kind vocabulary
    without thinking about the removal path) would silently start deleting them.
    """
    bindings = _FakeBindingStore({"DC-1": "local-a", "DC-2": "local-b"})
    # Claim it as managed anyway — the kind vocabulary, not the projection, must exclude it.
    local_a = _local("local-a", [(relation, "local-b")], [(relation, "local-b")])
    local_b = _local("local-b", [], [])
    snapshot = _enrich_via_fetcher({"DC-1": {}}, {"DC-1": []})

    records = _inbound(snapshot, bindings, {"local-a": local_a, "local-b": local_b})
    assert _removals(records) == [], (
        f"a {relation} dep was removed; that relation has no peer link type, so its absence "
        f"from the peer link set carries no information. records={json.dumps(records)}"
    )


# ---------------------------------------------------------------------------
# G4 — never mirror a removal for a link the outbound side is creating right now
# ---------------------------------------------------------------------------


def test_g4_outbound_add_this_pass_suppresses_the_removal() -> None:
    """G4: this is the guard that covers G3's blind spot, so it is not cosmetic.

    `add_managed_ref` is folded by the LINK-event processor (`reducer/_processors.py:411`), so
    a ref is "managed" the instant it is created LOCALLY — it does NOT mean "we pushed it to
    the peer". So G3 passes a brand-new local dep straight through, and the peer legitimately
    has no such link yet. In a healthy pass the outbound differ emits a link ADD for exactly
    that dep, and this suppression is what stops the inbound side from deleting it.

    Asserted deliberately against `link_add_keys` — the outbound record carries `to_key`
    (a peer key), which the suppression must map back from the inbound record's local
    `target_id`.
    """
    bindings, locals_by_id = _one_managed_dep()
    snapshot = _enrich_via_fetcher({"DC-1": {}}, {"DC-1": []})
    outbound = [_FakeOutbound("DC-1", [{"action": "add", "to_key": "DC-2", "relation": "blocks"}])]

    records = _inbound(snapshot, bindings, locals_by_id, outbound)
    assert _removals(records) == [], (
        "a removal was mirrored for a target the OUTBOUND side is adding this pass — that dep "
        f"is local-only-and-being-pushed, not peer-deleted. records={json.dumps(records)}"
    )


def test_g4_unrelated_outbound_add_does_not_suppress_the_removal() -> None:
    """G4 must be TARGETED. An outbound add to a DIFFERENT key must not blanket-suppress.

    Without this, a guard implemented as "any outbound link activity suppresses everything"
    would pass the cell above while disabling the feature whenever the pass has any link work.
    """
    bindings = _FakeBindingStore({"DC-1": "local-a", "DC-2": "local-b", "DC-3": "local-c"})
    local_a = _local("local-a", [("blocks", "local-b")], [("blocks", "local-b")])
    locals_by_id = {
        "local-a": local_a,
        "local-b": _local("local-b", [], []),
        "local-c": _local("local-c", [], []),
    }
    snapshot = _enrich_via_fetcher({"DC-1": {}}, {"DC-1": []})
    outbound = [_FakeOutbound("DC-1", [{"action": "add", "to_key": "DC-3", "relation": "blocks"}])]

    assert len(_removals(_inbound(snapshot, bindings, locals_by_id, outbound))) == 1, (
        "an outbound add to an UNRELATED key suppressed the removal; the guard must key on "
        "the target, not on the mere presence of outbound link activity"
    )


def test_g4_inbound_add_suppression_is_unchanged() -> None:
    """Regression: making the filter action-aware must not change the ADD precedence.

    An outbound link REMOVE (a deliberate local unlink) must still suppress the inbound ADD
    that would re-reflect the still-present peer link — remove-wins, so local wins and the
    unlink converges (bug wake-inn-parse).
    """
    bindings = _FakeBindingStore({"DC-1": "local-a", "DC-2": "local-b"})
    local_a = _local("local-a", [], [])  # no local dep -> the peer link is an inbound ADD
    locals_by_id = {"local-a": local_a, "local-b": _local("local-b", [], [])}
    snapshot = {"DC-1": {"issuelinks": [_blocks_outward("DC-2")]}}
    outbound = [
        _FakeOutbound("DC-1", [{"action": "remove", "to_key": "DC-2", "relation": "blocks"}])
    ]

    records = _inbound(snapshot, bindings, locals_by_id, outbound)
    assert records == [], (
        f"an outbound unlink no longer suppresses the inbound re-add echo; got {records!r}"
    )


# ---------------------------------------------------------------------------
# Steady state, and the relation-vocabulary trap
# ---------------------------------------------------------------------------


def test_steady_state_does_not_churn() -> None:
    """A managed dep STILL present on the peer emits no removal, on repeated passes."""
    bindings, locals_by_id = _one_managed_dep()
    snapshot = _enrich_via_fetcher({"DC-1": {}}, {"DC-1": [_blocks_outward("DC-2")]})

    for pass_no in (1, 2, 3):
        records = _inbound(snapshot, bindings, locals_by_id)
        assert _removals(records) == [], (
            f"pass {pass_no} emitted a removal for a link that is still on the peer — this "
            f"would churn every pass. records={json.dumps(records)}"
        )


def test_inward_blocks_is_not_a_spurious_removal() -> None:
    """The observed set must be built in LOCAL RELATION VOCABULARY, not from raw peer keys.

    Jira shows one blocking edge from both endpoints. On the far end it is `inwardIssue` +
    `Blocks`, which is `depends_on` locally — NOT `blocks`. Comparing raw keys, or comparing
    the un-inverted base relation, makes this link read as a different relation than the local
    dep and emits a removal for a link that is still there. That is the precise failure mode
    that every prior link fix in this file re-introduced in its mirror.
    """
    bindings = _FakeBindingStore({"DC-1": "local-a", "DC-2": "local-b"})
    # local-b depends_on local-a; the peer still carries the link, seen from local-b's side.
    local_b = _local("local-b", [("depends_on", "local-a")], [("depends_on", "local-a")])
    locals_by_id = {"local-a": _local("local-a", [], []), "local-b": local_b}
    snapshot = _enrich_via_fetcher({"DC-2": {}}, {"DC-2": [_blocks_inward("DC-1")]})

    records = _inbound(snapshot, bindings, locals_by_id)
    assert _removals(records) == [], (
        "an inward Blocks link was not recognised as the local `depends_on` dep, so a link "
        f"that is STILL on the peer was queued for deletion. records={json.dumps(records)}"
    )


# ---------------------------------------------------------------------------
# End to end — the record must survive the apply layer and the dep must be GONE
# ---------------------------------------------------------------------------


@pytest.fixture
def two_linked_tickets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, str]:
    """A real initialised store where `a` blocks `b`, so the dep and its managed ref exist."""
    import rebar

    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.com"),
        ("git", "config", "user.name", "d"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    a = str(rebar.create_ticket("task", "2b16 link source", repo_root=repo))
    b = str(rebar.create_ticket("task", "2b16 link target", repo_root=repo))
    rebar.link(a, b, "blocks", repo_root=repo)
    return repo, a, b


def _targets(repo: Path, ticket_id: str) -> set[str]:
    import rebar

    deps = rebar.show_ticket(ticket_id, repo_root=repo).get("deps") or []
    return {d.get("target_id") for d in deps}


def test_end_to_end_a_peer_deleted_link_leaves_the_dep_gone(
    two_linked_tickets: tuple[Path, str, str],
) -> None:
    """THE ACCEPTANCE ORACLE: differ emit -> payload -> apply -> the dep is POSITIVELY ABSENT.

    A differ-boundary oracle is not sufficient here and that is the whole reason this cell
    exists. `_inbound_update_apply_links` used to open `if entry.get("action") != "add":
    continue` (`apply_inbound_records.py:560-562`) and call only `rebar.link`, so a removal
    record was silently discarded — a differ-only assertion goes GREEN while the store is
    untouched, which is exactly the silent-failure class this epic exists to end.

    "The apply call did not raise" is deliberately NOT the oracle. The only thing read here is
    whether the dep is gone from the reduced ticket.
    """
    import rebar

    repo, a, b = two_linked_tickets

    # SETUP, asserted: the dep and its managed ref must really exist, or absence is vacuous.
    assert b in _targets(repo, a), f"SETUP FAILED: {a} does not carry a dep to {b}"
    managed = rebar.show_ticket(a, repo_root=repo).get("managed_refs") or []
    assert ["blocks", b] in [list(m) for m in managed], (
        f"SETUP FAILED: ('blocks', {b}) is not in managed_refs={managed!r}; the removal gate "
        f"would decline and this cell would pass for the wrong reason"
    )

    bindings = _FakeBindingStore({"DC-1": a, "DC-2": b})
    locals_by_id = {
        a: rebar.show_ticket(a, repo_root=repo),
        b: rebar.show_ticket(b, repo_root=repo),
    }
    # The peer was observed and the link is GONE.
    snapshot = _enrich_via_fetcher({"DC-1": {}}, {"DC-1": []})

    records = _inbound(snapshot, bindings, locals_by_id)
    assert _removals(records), (
        f"the differ emitted no removal, so the apply layer cannot be exercised: {records!r}"
    )

    apply_records = importlib.import_module("rebar_reconciler.apply_inbound_records")
    applied = apply_records._inbound_update_apply_links({"links": records}, a, repo)

    remaining = _targets(repo, a)
    assert b not in remaining, (
        f"THE DEP IS STILL THERE. The peer link was deleted and the removal record was "
        f"emitted, but {a} still carries a dep to {b} (deps target {sorted(remaining)}). The "
        f"record was dropped between the differ and the store; applied={applied}"
    )
    assert applied == 1, (
        f"the applier reported {applied} links applied for one removal — the record must be "
        f"counted, or the silent-no-op canary (apply_handlers.py:274-278) cannot see it"
    )


def test_end_to_end_reapply_is_idempotent(
    two_linked_tickets: tuple[Path, str, str],
) -> None:
    """Applying the same removal twice must not raise and must leave the dep absent.

    A pass can re-see the same peer-deleted link before the store settles, so the second
    apply must be a no-op rather than an error that aborts the remaining records.
    """
    repo, a, b = two_linked_tickets
    apply_records = importlib.import_module("rebar_reconciler.apply_inbound_records")
    payload = {"links": [{"action": "remove", "target_id": b, "relation": "blocks"}]}

    apply_records._inbound_update_apply_links(payload, a, repo)
    apply_records._inbound_update_apply_links(payload, a, repo)

    assert b not in _targets(repo, a), (
        f"the dep to {b} came back (or was never removed) after a repeated apply"
    )


# ---------------------------------------------------------------------------
# G5 — `rebar.unlink` is pair-scoped, so confirm the relation before removing
# ---------------------------------------------------------------------------


def test_g5_relation_mismatch_removes_nothing_and_logs(
    two_linked_tickets: tuple[Path, str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """G5 (e39f form): removal is RELATION-SCOPED, so a mismatch is a logged no-op.

    Links are written keyed on (target_id, relation), so a pair can hold two relations and
    the inbound removal must act on exactly the relation whose peer link vanished (ticket
    e39f-5055-f5af-424a). The safety invariant from ticket 2b16 is unchanged and asserted
    positively here: a record naming a relation with NO matching net-active local link — here
    the pair holds `blocks` but the record claims `relates_to` vanished — must remove NOTHING
    (never a link the peer still carries), must not be counted as applied, and the skip must
    be logged rather than passing silently.
    """
    repo, a, b = two_linked_tickets
    apply_records = importlib.import_module("rebar_reconciler.apply_inbound_records")

    # Pin the level ON THE ORIGINATING LOGGER: it lives outside the `rebar` hierarchy
    # that tests/conftest.py's propagation guard restores, so a leaked
    # setLevel(WARNING) from an earlier full-suite test would otherwise drop this
    # INFO record at the source and make the assertion below unreachable (bug 9ac2).
    with caplog.at_level("INFO", logger=apply_records.logger.name):
        applied = apply_records._inbound_update_apply_links(
            {"links": [{"action": "remove", "target_id": b, "relation": "relates_to"}]}, a, repo
        )

    assert b in _targets(repo, a), (
        "THE WRONG LINK WAS REMOVED. The pair holds only `blocks`, but a `relates_to` "
        "removal record deleted it anyway — that link is still on the peer"
    )
    assert applied == 0, f"a no-op removal must not be counted as applied (got {applied})"
    assert any("relates_to" in r.getMessage() for r in caplog.records), (
        "the no-matching-relation skip was silent; every defect in this family has been a "
        f"silent success. captured={[r.getMessage() for r in caplog.records]!r}"
    )


# ---------------------------------------------------------------------------
# e39f — a pair holding TWO relations converges when the peer drops ONE of them
# ---------------------------------------------------------------------------


def _relations_of(repo: Path, source: str, target: str) -> set[str]:
    import rebar

    deps = rebar.show_ticket(source, repo_root=repo).get("deps") or []
    return {d.get("relation") for d in deps if d.get("target_id") == target}


@pytest.fixture
def double_related_tickets(
    two_linked_tickets: tuple[Path, str, str],
) -> tuple[Path, str, str]:
    """`a` relates_to `b` ON TOP of the existing blocks link — two net-active relations.

    relates_to is linked SECOND so it is the pair's most-recent net-active link; the
    convergence cell below removes `blocks` (the OLDER one), which is exactly the case the
    pre-e39f pair-scoped G5 guard could only decline forever.
    """
    import rebar

    repo, a, b = two_linked_tickets
    rebar.link(a, b, "relates_to", repo_root=repo)
    return repo, a, b


def test_e39f_inbound_removal_converges_for_a_double_related_pair(
    double_related_tickets: tuple[Path, str, str],
) -> None:
    """THE e39f ACCEPTANCE ORACLE: the exactly-named relation is removed, the other survives.

    The pair holds blocks + relates_to and the most-recent net-active link is relates_to. The
    peer dropped `blocks`. The pre-e39f pair-scoped guard could only DECLINE this forever
    (the removal re-emitted and re-declined every pass — silent non-convergence). The ratified
    contract: the apply removes exactly the mirrored (target, relation) link, leaves the
    other relation net-active, and counts the apply so the silent-no-op canary sees it.
    """
    repo, a, b = double_related_tickets
    assert _relations_of(repo, a, b) == {"blocks", "relates_to"}, (
        "SETUP FAILED: the pair does not hold two net-active relations"
    )

    apply_records = importlib.import_module("rebar_reconciler.apply_inbound_records")
    applied = apply_records._inbound_update_apply_links(
        {"links": [{"action": "remove", "target_id": b, "relation": "blocks"}]}, a, repo
    )

    remaining = _relations_of(repo, a, b)
    assert "blocks" not in remaining, (
        "NON-CONVERGENCE. The peer dropped `blocks` but the local blocks link is still "
        f"there — the removal was declined instead of applied; remaining={sorted(remaining)}"
    )
    assert "relates_to" in remaining, (
        "THE WRONG LINK WAS REMOVED. The peer still carries relates_to but the local "
        "relates_to link is gone — relation-scoped removal must touch only the named relation"
    )
    assert applied == 1, (
        f"the applier reported {applied} for one converged removal — it must be counted"
    )


def test_e39f_double_related_reapply_is_idempotent(
    double_related_tickets: tuple[Path, str, str],
) -> None:
    """Re-applying the same relation-scoped removal is a counted-zero no-op."""
    repo, a, b = double_related_tickets
    apply_records = importlib.import_module("rebar_reconciler.apply_inbound_records")
    payload = {"links": [{"action": "remove", "target_id": b, "relation": "blocks"}]}

    apply_records._inbound_update_apply_links(payload, a, repo)
    applied_again = apply_records._inbound_update_apply_links(payload, a, repo)

    remaining = _relations_of(repo, a, b)
    assert "blocks" not in remaining and "relates_to" in remaining, (
        f"a repeated apply changed the outcome; remaining={sorted(remaining)}"
    )
    assert applied_again == 0, (
        f"a no-op re-apply must not be counted as applied (got {applied_again})"
    )
