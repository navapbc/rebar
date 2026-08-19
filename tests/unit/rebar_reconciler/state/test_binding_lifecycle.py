"""Direct oracles for ``BindingLifecycle`` identity transitions (RP-02 S2 T1).

``dermatoid-brassy-junco``. This is the FIRST lifecycle slice: bind/confirm/unbind, stale
reverse cleanup, and the immutable-numeric-id re-key. Absence, retirement, tombstones and
comment identity belong to S2 T2 and are deliberately absent here.

Every transition is asserted on BOTH indexes and then RELOADED through a real
``BindingRepository``, because a policy that updates only the in-memory forward entry looks
correct until the next pass reads the file back. The characterization source is
``BindingStore.bind_pending`` / ``record_pending_key`` / ``bind_confirm`` / ``unbind`` /
``get_jira_id`` / ``record_jira_id`` and step 4 of ``note_absent_or_rekey`` on the reviewed
base; behaviour must match those exactly.

Two things this suite deliberately does NOT assert, because they are not existing behaviour
(verified by search: no ambiguity/abort handling exists in ``binding_store.py``,
``binding_repository.py`` or ``peer_state.py``):

* there is no ambiguity detection and no safety-abort on a double bind — ``bind_confirm`` is
  last-writer-wins and pops exactly the one differing old reverse key;
* nothing sweeps or repairs the reverse index outside the enumerated permitted cases.

The at-most-one-identity invariant holds because of those targeted single-key pops, and that
is what is pinned. The term "safety-abort" is reserved for RP-02 S3.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rebar_reconciler.binding_lifecycle import BindingLifecycle
from rebar_reconciler.binding_repository import BindingRepository


def _tracker(root: Path) -> Path:
    return root / ".tickets-tracker"


def _bridge(root: Path) -> Path:
    return _tracker(root) / ".bridge_state"


def _lifecycle(root: Path) -> tuple[BindingLifecycle, BindingRepository]:
    repo = BindingRepository(_tracker(root))
    return BindingLifecycle(repo), repo


def _indexes(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The persisted forward and reverse indexes, read back from disk."""
    raw = json.loads((_bridge(root) / "bindings.json").read_text(encoding="utf-8"))
    return raw["bindings"], raw["reverse"]


def _assert_one_to_one(root: Path) -> None:
    """The core invariant: at most one forward entry per local id, and every reverse key
    points at a local id whose forward entry carries that same key."""
    bindings, reverse = _indexes(root)
    for jira_key, local_id in reverse.items():
        entry = bindings.get(local_id)
        assert entry is not None, f"reverse[{jira_key}] dangles at unknown local {local_id}"
        assert entry.get("jira_key") == jira_key, (
            f"reverse[{jira_key}] -> {local_id}, but its forward key is {entry.get('jira_key')!r}"
        )
    keyed = [e.get("jira_key") for e in bindings.values() if e.get("jira_key")]
    assert len(keyed) == len(set(keyed)), f"a jira_key is bound to two local ids: {keyed}"


# ---------------------------------------------------------------------------
# pending -> keyed-pending -> confirmed
# ---------------------------------------------------------------------------


def test_bind_pending_creates_a_keyless_pending_entry(tmp_path: Path) -> None:
    """Step 1 of the write-ahead protocol: a durable pending record with NO key yet. It is
    what recovery keys on, so it must exist before any Jira create is attempted."""
    lifecycle, repo = _lifecycle(tmp_path)

    lifecycle.bind_pending("loc-A")
    repo.save()

    bindings, reverse = _indexes(tmp_path)
    assert bindings["loc-A"]["state"] == "pending"
    assert bindings["loc-A"]["jira_key"] is None
    assert bindings["loc-A"]["created_at"]
    assert bindings["loc-A"]["updated_at"]
    assert reverse == {}, "a keyless pending entry must not create a reverse entry"
    _assert_one_to_one(tmp_path)


def test_record_pending_key_keeps_the_entry_pending(tmp_path: Path) -> None:
    """Step 3: the key is recorded while the entry is STILL pending, before the rebar-id
    label is attached. That sub-state is what makes recovery deterministic (retro-attach and
    confirm, with no Jira search, so a crash in the create->label window yields no
    duplicate). Promoting to confirmed here would lose that distinction."""
    lifecycle, repo = _lifecycle(tmp_path)
    lifecycle.bind_pending("loc-A")

    lifecycle.record_pending_key("loc-A", "DIG-A")
    repo.save()

    bindings, reverse = _indexes(tmp_path)
    assert bindings["loc-A"]["state"] == "pending"
    assert bindings["loc-A"]["jira_key"] == "DIG-A"
    assert reverse == {}, "a keyed-PENDING entry must not yet be in the reverse index"
    _assert_one_to_one(tmp_path)


def test_record_pending_key_creates_an_entry_defensively(tmp_path: Path) -> None:
    """No prior pending entry is a defensive case, not an error: one is created, pending."""
    lifecycle, repo = _lifecycle(tmp_path)

    lifecycle.record_pending_key("loc-Z", "DIG-Z")
    repo.save()

    bindings, _ = _indexes(tmp_path)
    assert bindings["loc-Z"]["state"] == "pending"
    assert bindings["loc-Z"]["jira_key"] == "DIG-Z"
    assert bindings["loc-Z"]["created_at"]


def test_bind_confirm_populates_both_indexes(tmp_path: Path) -> None:
    lifecycle, repo = _lifecycle(tmp_path)
    lifecycle.bind_pending("loc-A")
    lifecycle.record_pending_key("loc-A", "DIG-A")

    lifecycle.bind_confirm("loc-A", "DIG-A")
    repo.save()

    bindings, reverse = _indexes(tmp_path)
    assert bindings["loc-A"]["state"] == "confirmed"
    assert bindings["loc-A"]["jira_key"] == "DIG-A"
    assert reverse == {"DIG-A": "loc-A"}
    _assert_one_to_one(tmp_path)


def test_bind_confirm_without_a_prior_entry_is_allowed_for_recovery(tmp_path: Path) -> None:
    """Recovery confirms directly, with no pending predecessor."""
    lifecycle, repo = _lifecycle(tmp_path)

    lifecycle.bind_confirm("loc-R", "DIG-R")
    repo.save()

    bindings, reverse = _indexes(tmp_path)
    assert bindings["loc-R"]["state"] == "confirmed"
    assert bindings["loc-R"]["created_at"]
    assert reverse == {"DIG-R": "loc-R"}


def test_bind_confirm_is_idempotent_on_the_same_key(tmp_path: Path) -> None:
    lifecycle, repo = _lifecycle(tmp_path)
    lifecycle.bind_confirm("loc-A", "DIG-A")
    repo.save()
    first = (_bridge(tmp_path) / "bindings.json").read_bytes()

    lifecycle.bind_confirm("loc-A", "DIG-A")
    repo.save()

    bindings, reverse = _indexes(tmp_path)
    assert reverse == {"DIG-A": "loc-A"}
    assert len(bindings) == 1
    _assert_one_to_one(tmp_path)
    assert first  # the first save really happened, so this is a re-confirm not a first write


# ---------------------------------------------------------------------------
# permitted stale-reverse removals
# ---------------------------------------------------------------------------


def test_bind_confirm_rebind_drops_only_the_differing_old_reverse_key(tmp_path: Path) -> None:
    """A rebind (hard-delete then re-create binds the same local id to a NEW key) must drop
    the OLD reverse entry in the SAME operation, or reverse[old] dangles at this local id
    forever — there is no dedicated rebind method, and only unbind used to clean it.

    Last-writer-wins with ONE targeted pop: no sweep, no ambiguity detection, no abort.
    """
    lifecycle, repo = _lifecycle(tmp_path)
    lifecycle.bind_confirm("loc-A", "DIG-OLD")
    lifecycle.bind_confirm("loc-B", "DIG-KEEP")
    repo.save()

    lifecycle.bind_confirm("loc-A", "DIG-NEW")
    repo.save()

    bindings, reverse = _indexes(tmp_path)
    assert bindings["loc-A"]["jira_key"] == "DIG-NEW"
    assert reverse == {"DIG-NEW": "loc-A", "DIG-KEEP": "loc-B"}
    assert "DIG-OLD" not in reverse
    _assert_one_to_one(tmp_path)


def test_unbind_clears_both_indexes_and_sweeps_orphans(tmp_path: Path) -> None:
    """Unbind must not gate the reverse cleanup on the forward entry it has just destroyed:
    a keyless forward entry once stranded its reverse key permanently, reported by
    `bridge fsck` as reverse_missing_forward forever. The keyed pop is the O(1) fast path;
    the sweep then clears any reverse key still pointing at this local id — including one
    orphaned out of band, which is what `bridge fsck --repair`'s prune verb relies on."""
    lifecycle, repo = _lifecycle(tmp_path)
    lifecycle.bind_confirm("loc-A", "DIG-A")
    lifecycle.bind_confirm("loc-B", "DIG-B")
    # An out-of-band orphan: a reverse key pointing at loc-A that the forward entry does not
    # name (a prune, a manual edit, a merge=ours artifact).
    repo.reverse["DIG-ORPHAN"] = "loc-A"
    repo.save()

    lifecycle.unbind("loc-A")
    repo.save()

    bindings, reverse = _indexes(tmp_path)
    assert "loc-A" not in bindings
    assert reverse == {"DIG-B": "loc-B"}, "both the keyed entry and the orphan must be cleared"
    _assert_one_to_one(tmp_path)


def test_unbind_of_a_keyless_entry_still_clears_its_reverse_key(tmp_path: Path) -> None:
    """The 874a regression: the forward entry has no jira_key, so only the sweep can find
    the reverse entry. Gating on the forward key stranded it forever."""
    lifecycle, repo = _lifecycle(tmp_path)
    lifecycle.bind_pending("loc-A")
    repo.reverse["DIG-STRANDED"] = "loc-A"
    repo.save()

    lifecycle.unbind("loc-A")
    repo.save()

    bindings, reverse = _indexes(tmp_path)
    assert "loc-A" not in bindings
    assert reverse == {}
    _assert_one_to_one(tmp_path)


def test_unbind_of_an_unknown_local_id_is_a_noop(tmp_path: Path) -> None:
    lifecycle, repo = _lifecycle(tmp_path)
    lifecycle.bind_confirm("loc-A", "DIG-A")
    repo.save()
    before = (_bridge(tmp_path) / "bindings.json").read_bytes()

    lifecycle.unbind("loc-MISSING")
    repo.save()

    assert (_bridge(tmp_path) / "bindings.json").read_bytes() == before


# ---------------------------------------------------------------------------
# immutable numeric id and the re-key seam (bug 7c26)
# ---------------------------------------------------------------------------


def test_record_and_read_the_immutable_numeric_id(tmp_path: Path) -> None:
    """A Jira issue's KEY changes when it moves between projects; its numeric id never does.
    Capture is purely additive, so it is a separate operation rather than a parameter on the
    write-ahead methods (this store is shared with Cloud)."""
    lifecycle, repo = _lifecycle(tmp_path)
    lifecycle.bind_confirm("loc-A", "DIG-A")

    lifecycle.record_jira_id("loc-A", "10001")
    repo.save()

    assert lifecycle.get_jira_id("loc-A") == "10001"
    bindings, _ = _indexes(tmp_path)
    assert bindings["loc-A"]["jira_id"] == "10001"


def test_missing_numeric_id_reads_as_none_without_rewriting(tmp_path: Path) -> None:
    """None is VALID and means "not captured yet" — every binding written before bug 7c26 has
    no id. The absence path degrades to its pre-7c26 behaviour, so no migration is required."""
    bridge = _bridge(tmp_path)
    bridge.mkdir(parents=True)
    legacy = {
        "version": 2,
        "bindings": {"loc-L": {"jira_key": "DIG-L", "state": "confirmed"}},
        "reverse": {"DIG-L": "loc-L"},
    }
    (bridge / "bindings.json").write_text(json.dumps(legacy), encoding="utf-8")
    before = (bridge / "bindings.json").read_bytes()

    lifecycle, _ = _lifecycle(tmp_path)

    assert lifecycle.get_jira_id("loc-L") is None
    assert lifecycle.get_jira_id("loc-ABSENT") is None
    assert (bridge / "bindings.json").read_bytes() == before, "reading must not rewrite"


def test_record_jira_id_is_a_noop_for_unbound_empty_or_unchanged(tmp_path: Path) -> None:
    lifecycle, repo = _lifecycle(tmp_path)
    lifecycle.bind_confirm("loc-A", "DIG-A")
    lifecycle.record_jira_id("loc-A", "10001")
    repo.save()
    before = (_bridge(tmp_path) / "bindings.json").read_bytes()

    lifecycle.record_jira_id("loc-MISSING", "999")
    lifecycle.record_jira_id("loc-A", "")
    lifecycle.record_jira_id("loc-A", "10001")
    repo.save()

    assert (_bridge(tmp_path) / "bindings.json").read_bytes() == before
    assert lifecycle.get_jira_id("loc-A") == "10001"


def test_rekey_swaps_both_indexes_and_resets_absence(tmp_path: Path) -> None:
    """The re-key rule: a bound key can stop resolving because the issue was DELETED or
    because it was MOVED and re-keyed. Asking by the one identifier a move cannot change
    tells them apart. On a hit whose key differs, the reverse index must be updated in the
    SAME operation, or the old key keeps resolving to this local id and re-detaches next
    pass; and the absence counter must reset, because the issue is PRESENT."""
    lifecycle, repo = _lifecycle(tmp_path)
    lifecycle.bind_confirm("loc-A", "DIG-OLD")
    lifecycle.record_jira_id("loc-A", "10001")
    repo.bindings["loc-A"]["absent_404_count"] = 2
    repo.save()

    assert lifecycle.rekey("DIG-OLD", "DIG-NEW") is True
    repo.save()

    bindings, reverse = _indexes(tmp_path)
    assert bindings["loc-A"]["jira_key"] == "DIG-NEW"
    assert bindings["loc-A"]["absent_404_count"] == 0
    assert bindings["loc-A"]["jira_id"] == "10001", "the immutable id must not change on a move"
    assert reverse == {"DIG-NEW": "loc-A"}
    assert "DIG-OLD" not in reverse
    _assert_one_to_one(tmp_path)


def test_rekey_of_an_unbound_key_is_false_and_changes_nothing(tmp_path: Path) -> None:
    lifecycle, repo = _lifecycle(tmp_path)
    lifecycle.bind_confirm("loc-A", "DIG-A")
    repo.save()
    before = (_bridge(tmp_path) / "bindings.json").read_bytes()

    assert lifecycle.rekey("DIG-UNKNOWN", "DIG-OTHER") is False
    repo.save()

    assert (_bridge(tmp_path) / "bindings.json").read_bytes() == before


def test_entry_for_jira_key_resolves_through_the_reverse_index(tmp_path: Path) -> None:
    lifecycle, _ = _lifecycle(tmp_path)
    lifecycle.bind_confirm("loc-A", "DIG-A")

    entry = lifecycle.entry_for_jira_key("DIG-A")

    assert entry is not None
    assert entry["jira_key"] == "DIG-A"
    assert lifecycle.entry_for_jira_key("DIG-MISSING") is None


def test_lifecycle_operates_on_the_repository_dictionaries_not_copies(tmp_path: Path) -> None:
    """The policy owner must mutate the repository's OWN dictionaries. If it held copies, the
    repository would serialize state the policy never touched and every transition would be
    silently lost at save time."""
    lifecycle, repo = _lifecycle(tmp_path)

    lifecycle.bind_confirm("loc-A", "DIG-A")

    assert repo.bindings["loc-A"]["jira_key"] == "DIG-A"
    assert repo.reverse["DIG-A"] == "loc-A"
    assert repo.data["bindings"] is repo.bindings


def test_unknown_fields_on_a_binding_survive_a_transition(tmp_path: Path) -> None:
    """A transition must not strip fields it does not recognize — the file is shared with
    other writers, and peer_state/baseline data lives on the same entry."""
    bridge = _bridge(tmp_path)
    bridge.mkdir(parents=True)
    seeded = {
        "version": 2,
        "bindings": {
            "loc-A": {
                "jira_key": "DIG-A",
                "state": "confirmed",
                "future_field": {"keep": True},
                "baseline": {"summary": "s"},
            }
        },
        "reverse": {"DIG-A": "loc-A"},
    }
    (bridge / "bindings.json").write_text(json.dumps(seeded), encoding="utf-8")
    lifecycle, repo = _lifecycle(tmp_path)

    lifecycle.bind_confirm("loc-A", "DIG-A2")
    repo.save()

    bindings, _ = _indexes(tmp_path)
    assert bindings["loc-A"]["future_field"] == {"keep": True}
    assert bindings["loc-A"]["baseline"] == {"summary": "s"}


# ---------------------------------------------------------------------------
# facade compatibility: the public contract must not move
# ---------------------------------------------------------------------------


def test_facade_delegates_and_keeps_its_observable_contract(tmp_path: Path) -> None:
    """The reconciler, adapters and `bridge fsck` all bind to `BindingStore`. Extraction is
    only safe if the facade's answers are unchanged, so this exercises the same transitions
    through the real public entry point and reloads them."""
    from rebar_reconciler.binding_store import BindingStore

    store = BindingStore(_tracker(tmp_path))
    store.bind_pending("loc-P")
    store.record_pending_key("loc-K", "DIG-K")
    store.bind_confirm("loc-C", "DIG-C")
    store.record_jira_id("loc-C", "10002")
    store.save()

    reloaded = BindingStore(_tracker(tmp_path))
    assert reloaded.is_pending("loc-P") is True
    assert reloaded.get_jira_key("loc-P") is None
    assert reloaded.is_pending("loc-K") is True
    assert reloaded.get_jira_key("loc-K") == "DIG-K"
    assert reloaded.get_jira_key("loc-C") == "DIG-C"
    assert reloaded.get_local_id("DIG-C") == "loc-C"
    assert reloaded.get_jira_id("loc-C") == "10002"
    assert reloaded.confirmed_count() == 1
    assert sorted(reloaded.pending_bindings()) == ["loc-K", "loc-P"]
    _assert_one_to_one(tmp_path)


def test_facade_rekey_falls_through_to_absence_when_the_key_is_unchanged(
    tmp_path: Path,
) -> None:
    """The seam split: `note_absent_or_rekey` keeps the client lookup and the absence
    fall-through on the facade, and delegates only the re-key mutation. A same-key answer
    must still record an absence exactly as `note_absent` always did — the branch outcomes
    do not move in this slice."""
    from rebar_reconciler.binding_store import BindingStore

    class _SameKeyClient:
        def get_issue_by_rest(self, jira_id: str) -> dict[str, Any]:
            return {"key": "DIG-A"}

    store = BindingStore(_tracker(tmp_path))
    store.bind_confirm("loc-A", "DIG-A")
    store.record_jira_id("loc-A", "10001")
    store.save()

    assert store.note_absent_or_rekey("DIG-A", _SameKeyClient()) is False

    assert store.all_bindings()["loc-A"]["absent_404_count"] == 1
    assert store.get_jira_key("loc-A") == "DIG-A"


def test_facade_rekey_reports_a_move_and_updates_both_indexes(tmp_path: Path) -> None:
    """A DIFFERENT current key means the issue moved: report PRESENT (True), re-key, and do
    not record an absence."""
    from rebar_reconciler.binding_store import BindingStore

    class _MovedClient:
        def get_issue_by_rest(self, jira_id: str) -> dict[str, Any]:
            return {"key": "DIG-MOVED"}

    store = BindingStore(_tracker(tmp_path))
    store.bind_confirm("loc-A", "DIG-OLD")
    store.record_jira_id("loc-A", "10001")
    store.save()

    assert store.note_absent_or_rekey("DIG-OLD", _MovedClient()) is True

    assert store.get_jira_key("loc-A") == "DIG-MOVED"
    assert store.get_local_id("DIG-MOVED") == "loc-A"
    assert store.get_local_id("DIG-OLD") is None
    assert store.all_bindings()["loc-A"].get("absent_404_count") == 0
    _assert_one_to_one(tmp_path)


@pytest.mark.parametrize(
    ("client", "reason"),
    [
        (None, "no client at all degrades to pre-7c26 behaviour"),
        (object(), "a client predating get_issue_by_rest degrades the same way"),
    ],
)
def test_facade_rekey_degrades_to_absence_without_a_usable_client(
    tmp_path: Path, client: Any, reason: str
) -> None:
    """DEGRADES TO TODAY'S BEHAVIOUR in every case it cannot disprove an absence. That is
    what makes the move-detection safe on the shared Cloud path and on an unmigrated store:
    it can only ADD a save, never skip an absence it did not disprove."""
    from rebar_reconciler.binding_store import BindingStore

    store = BindingStore(_tracker(tmp_path))
    store.bind_confirm("loc-A", "DIG-A")
    store.record_jira_id("loc-A", "10001")
    store.save()

    assert store.note_absent_or_rekey("DIG-A", client) is False, reason

    assert store.all_bindings()["loc-A"]["absent_404_count"] == 1


def test_facade_rekey_degrades_to_absence_for_a_legacy_entry_with_no_numeric_id(
    tmp_path: Path,
) -> None:
    """Every pre-7c26 binding has no captured id, so the move question cannot be asked and
    the absence is recorded unchanged."""
    from rebar_reconciler.binding_store import BindingStore

    class _Client:
        def get_issue_by_rest(self, jira_id: str) -> dict[str, Any]:
            raise AssertionError("must not be consulted without a captured numeric id")

    store = BindingStore(_tracker(tmp_path))
    store.bind_confirm("loc-A", "DIG-A")
    store.save()

    assert store.note_absent_or_rekey("DIG-A", _Client()) is False

    assert store.all_bindings()["loc-A"]["absent_404_count"] == 1


def test_bind_confirm_rebind_does_not_sweep_an_out_of_band_orphan(tmp_path: Path) -> None:
    """The pop is TARGETED, not a sweep — and this is what distinguishes the two.

    `bind_confirm` removes only the one old key the forward entry itself named. A reverse
    key pointing at the same local id that the entry never named (an out-of-band orphan from
    a prune, a manual edit, a `merge=ours` artifact) is left ALONE. That is the reviewed
    base's behaviour, so a rebind can legitimately leave the reverse index one-to-many —
    which is precisely why `unbind` carries a sweep and why `bridge fsck` reports
    `reverse_missing_forward` at all. Note this test deliberately does NOT assert the
    one-to-one invariant: proving that the orphan SURVIVES is the point.

    Found by mutation: replacing the targeted pop with a sweep keyed on `local_id` is
    invisible to a fixture whose only other reverse entry belongs to a different local id.
    """
    lifecycle, repo = _lifecycle(tmp_path)
    lifecycle.bind_confirm("loc-A", "DIG-OLD")
    repo.reverse["DIG-ORPHAN"] = "loc-A"
    repo.save()

    lifecycle.bind_confirm("loc-A", "DIG-NEW")
    repo.save()

    _, reverse = _indexes(tmp_path)
    assert reverse["DIG-NEW"] == "loc-A", "the new key is bound"
    assert "DIG-OLD" not in reverse, "the key the entry named is popped"
    assert reverse.get("DIG-ORPHAN") == "loc-A", (
        "bind_confirm must NOT sweep; only unbind does. Sweeping here would silently change "
        "behaviour and mask the fsck-reportable state this code deliberately leaves."
    )
