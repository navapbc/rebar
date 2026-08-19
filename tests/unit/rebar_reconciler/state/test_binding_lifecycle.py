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


# ===========================================================================
# RP-02 S2 T2 (sportive-statued-goose) — absence, tombstone, comment identity.
#
# These are DIRECT oracles on the policy owner. Facade-level coverage already
# exists and stays the regression oracle for the extraction
# (state/test_binding_absence_lifecycle.py, test_binding_store_comment_ids.py,
# state/test_corrupt_state_guard.py, test_no_resurrection_after_confirmed_delete_3b5f.py);
# these add the both-index / reload-through-the-repository tier the facade tests
# do not reach, and they pin the fail-open vs fail-closed asymmetry that makes
# this cluster dangerous to move.
#
# Nothing here may change a THRESHOLD, a stored field, or a disposition. Three
# confirmed 404s retire; a 200 resets; retired corruption and alert-write
# failures stay fail-open; live corruption stays fail-closed.
# ===========================================================================

_RETIRE_GRACE_ENV = "RECONCILER_ABSENT_RETIRE_GRACE"


def _retired_file(root: Path) -> dict[str, Any]:
    raw = json.loads((_bridge(root) / "bindings-retired.json").read_text(encoding="utf-8"))
    return raw["retired"]


def _alerts(root: Path) -> list[dict[str, Any]]:
    d = root / "bridge_state" / "bridge_alerts"
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(d.glob("*.jsonl")):
        out += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return out


def _bind_confirmed(root: Path) -> tuple[BindingLifecycle, BindingRepository]:
    lifecycle, repo = _lifecycle(root)
    lifecycle.bind_confirm("loc-A", "DIG-A")
    repo.save()
    return lifecycle, repo


# -- absence bookkeeping ----------------------------------------------------


def test_note_absent_increments_and_retires_at_the_third_consecutive_404(
    tmp_path: Path,
) -> None:
    """THREE consecutive 404s is the retirement threshold, and it must not move. A single
    404 is exactly what a lagging index or a transient produces for an issue that DOES
    exist, so absence has to be corroborated before a binding is soft-deleted."""
    lifecycle, repo = _bind_confirmed(tmp_path)

    lifecycle.note_absent("DIG-A")
    assert repo.bindings["loc-A"]["absent_404_count"] == 1
    assert lifecycle.is_retired("DIG-A") is False
    lifecycle.note_absent("DIG-A")
    assert repo.bindings["loc-A"]["absent_404_count"] == 2
    assert lifecycle.is_retired("DIG-A") is False

    lifecycle.note_absent("DIG-A")

    assert lifecycle.is_retired("DIG-A") is True
    assert "loc-A" not in repo.bindings, "retirement UNBINDS the local ticket"
    assert "DIG-A" not in repo.reverse
    assert _retired_file(tmp_path)["DIG-A"]["local_id"] == "loc-A"


def test_clear_absent_resets_the_counter_after_a_successful_read(tmp_path: Path) -> None:
    """A 200 means the issue is alive, so the corroboration restarts from zero — otherwise
    three 404s spread across unrelated passes would eventually retire a live issue."""
    lifecycle, repo = _bind_confirmed(tmp_path)
    lifecycle.note_absent("DIG-A")
    lifecycle.note_absent("DIG-A")

    lifecycle.clear_absent("DIG-A")

    assert repo.bindings["loc-A"]["absent_404_count"] == 0
    lifecycle.note_absent("DIG-A")
    lifecycle.note_absent("DIG-A")
    assert lifecycle.is_retired("DIG-A") is False, "the reset must have restarted the count"


def test_clear_absent_does_not_dirty_an_entry_with_no_absence(tmp_path: Path) -> None:
    """No counter means nothing to reset; touching updated_at would churn the committed
    file on every healthy pass."""
    lifecycle, repo = _bind_confirmed(tmp_path)
    before = dict(repo.bindings["loc-A"])

    lifecycle.clear_absent("DIG-A")

    assert repo.bindings["loc-A"] == before


def test_note_absent_and_clear_absent_ignore_an_unbound_key(tmp_path: Path) -> None:
    lifecycle, repo = _bind_confirmed(tmp_path)
    before = (_bridge(tmp_path) / "bindings.json").read_bytes()

    lifecycle.note_absent("DIG-UNKNOWN")
    lifecycle.clear_absent("DIG-UNKNOWN")
    repo.save()

    assert (_bridge(tmp_path) / "bindings.json").read_bytes() == before


def test_retirement_survives_a_reload_through_the_repository(tmp_path: Path) -> None:
    """The tombstone and the unbinding must both be durable, or the next pass re-creates
    the issue it just confirmed deleted."""
    lifecycle, _ = _bind_confirmed(tmp_path)
    for _ in range(3):
        lifecycle.note_absent("DIG-A")

    reloaded, repo2 = _lifecycle(tmp_path)

    assert reloaded.is_retired("DIG-A") is True
    assert reloaded.retired_key_for_local("loc-A") == "DIG-A"
    assert reloaded.is_retired_local("loc-A") is True
    assert "loc-A" not in repo2.bindings


# -- the retirement grace is configurable, and parsed defensively -----------


def test_retire_grace_env_override_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The threshold is an ops knob. Its sourcing must stay byte-identical through the
    extraction: a direct ambient read, not a config-seam lookup."""
    monkeypatch.setenv(_RETIRE_GRACE_ENV, "1")
    lifecycle, _ = _bind_confirmed(tmp_path)

    lifecycle.note_absent("DIG-A")

    assert lifecycle.is_retired("DIG-A") is True, "a grace of 1 must retire on the first 404"


@pytest.mark.parametrize("raw", ["abc", "", "1.5", "-3"])
def test_malformed_retire_grace_falls_back_to_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """A typo'd ops value must NOT abort the pass — it degrades to the documented default
    of 3, and any value below 1 is clamped to 1. This defensive parse is load-bearing:
    aborting a reconcile on a bad env var would be a worse failure than ignoring it."""
    monkeypatch.setenv(_RETIRE_GRACE_ENV, raw)
    lifecycle, _ = _bind_confirmed(tmp_path)

    lifecycle.note_absent("DIG-A")
    lifecycle.note_absent("DIG-A")
    retired_early = lifecycle.is_retired("DIG-A")
    lifecycle.note_absent("DIG-A")

    if raw == "-3":
        assert retired_early is True, "a negative value clamps to a minimum of 1"
    else:
        assert retired_early is False, f"{raw!r} must fall back to the default of 3"
        assert lifecycle.is_retired("DIG-A") is True


# -- tombstones: suppression and the documented route back ------------------


def test_retire_emits_an_alert_naming_the_binding(tmp_path: Path) -> None:
    """A soft delete is invisible in the live store afterwards, so it must be loud."""
    lifecycle, _ = _bind_confirmed(tmp_path)

    for _ in range(3):
        lifecycle.note_absent("DIG-A")

    retired = [a for a in _alerts(tmp_path) if a.get("kind") == "binding-retired"]
    assert len(retired) == 1
    assert retired[0]["jira_key"] == "DIG-A"
    assert retired[0]["local_id"] == "loc-A"


def test_note_create_suppressed_alerts_and_names_the_remedy(tmp_path: Path) -> None:
    """A suppression is work NOT done, and a silently-skipped create looks identical to a
    healthy steady state. The alert must name the route back so an operator never has to
    hand-edit the retired file."""
    lifecycle, _ = _lifecycle(tmp_path)

    lifecycle.note_create_suppressed("loc-A", "DIG-A")

    rec = [a for a in _alerts(tmp_path) if a.get("kind") == "outbound-create-suppressed"]
    assert len(rec) == 1
    assert rec[0]["local_id"] == "loc-A"
    assert rec[0]["jira_key"] == "DIG-A"
    assert "unretire" in rec[0]["remedy"]


def test_unretire_lifts_the_tombstone_from_both_the_set_and_the_file(tmp_path: Path) -> None:
    """The documented route back. Without it, suppression would be a permanent dead end
    escapable only by hand-editing bindings-retired.json."""
    lifecycle, _ = _bind_confirmed(tmp_path)
    for _ in range(3):
        lifecycle.note_absent("DIG-A")
    assert lifecycle.is_retired("DIG-A") is True

    assert lifecycle.unretire("DIG-A") is True

    assert lifecycle.is_retired("DIG-A") is False
    assert lifecycle.retired_key_for_local("loc-A") is None
    assert lifecycle.is_retired_local("loc-A") is False
    assert "DIG-A" not in _retired_file(tmp_path)
    reloaded, _ = _lifecycle(tmp_path)
    assert reloaded.is_retired("DIG-A") is False, "the lift must be durable"


def test_unretire_of_an_unretired_key_is_an_idempotent_noop(tmp_path: Path) -> None:
    lifecycle, _ = _bind_confirmed(tmp_path)

    assert lifecycle.unretire("DIG-NEVER-RETIRED") is False


def test_unretire_emits_an_alert(tmp_path: Path) -> None:
    lifecycle, _ = _bind_confirmed(tmp_path)
    for _ in range(3):
        lifecycle.note_absent("DIG-A")

    lifecycle.unretire("DIG-A")

    rec = [a for a in _alerts(tmp_path) if a.get("kind") == "binding-unretired"]
    assert len(rec) == 1
    assert rec[0]["jira_key"] == "DIG-A"


def test_retiring_one_key_leaves_other_tombstones_intact(tmp_path: Path) -> None:
    """The retired file is rewritten wholesale on each retirement, so a second retirement
    must not drop the first."""
    lifecycle, repo = _lifecycle(tmp_path)
    lifecycle.bind_confirm("loc-A", "DIG-A")
    lifecycle.bind_confirm("loc-B", "DIG-B")
    repo.save()

    for _ in range(3):
        lifecycle.note_absent("DIG-A")
    for _ in range(3):
        lifecycle.note_absent("DIG-B")

    assert set(_retired_file(tmp_path)) == {"DIG-A", "DIG-B"}
    assert lifecycle.is_retired("DIG-A") and lifecycle.is_retired("DIG-B")
    assert lifecycle.retired_key_for_local("loc-A") == "DIG-A"
    assert lifecycle.retired_key_for_local("loc-B") == "DIG-B"


def test_unretiring_one_key_leaves_other_tombstones_intact(tmp_path: Path) -> None:
    lifecycle, repo = _lifecycle(tmp_path)
    lifecycle.bind_confirm("loc-A", "DIG-A")
    lifecycle.bind_confirm("loc-B", "DIG-B")
    repo.save()
    for _ in range(3):
        lifecycle.note_absent("DIG-A")
    for _ in range(3):
        lifecycle.note_absent("DIG-B")

    lifecycle.unretire("DIG-A")

    assert lifecycle.is_retired("DIG-A") is False
    assert lifecycle.is_retired("DIG-B") is True, "an unrelated tombstone must survive"
    assert set(_retired_file(tmp_path)) == {"DIG-B"}
    assert lifecycle.retired_key_for_local("loc-B") == "DIG-B"


# -- comment identity: append-only and change-gated -------------------------


def test_record_comment_id_persists_immediately(tmp_path: Path) -> None:
    """Unlike the other capture operations this saves NOW (write-ahead): it is called on a
    successful add_comment return, and the durable entry is what the outbound differ's
    primary skip keys on, so a crash after the Jira post cannot re-post."""
    lifecycle, _ = _lifecycle(tmp_path)

    lifecycle.record_comment_id("hlc-1", "10001")

    raw = json.loads((_bridge(tmp_path) / "bindings.json").read_text(encoding="utf-8"))
    assert raw["comment_ids"] == {"hlc-1": "10001"}
    assert lifecycle.comment_id_for("hlc-1") == "10001"
    assert lifecycle.is_comment_mapped("hlc-1") is True


def test_identical_re_record_performs_no_write_at_all(tmp_path: Path) -> None:
    """Append-only and idempotent: a repeat must not WRITE, not merely produce equal bytes.

    Counting writes is the only oracle that works here. `record_comment_id` stamps no
    timestamp, so an unconditional save re-serializes byte-IDENTICAL content and a
    before/after byte comparison passes either way — which is exactly the tautology
    mutation exposed. The contract is "no save churn", i.e. no write, so the write is what
    is counted.
    """
    lifecycle, repo = _lifecycle(tmp_path)
    lifecycle.record_comment_id("hlc-1", "10001")
    before = (_bridge(tmp_path) / "bindings.json").read_bytes()

    writes: list[str] = []
    real_save = type(repo).save

    def counting_save(self: Any) -> None:
        writes.append("save")
        real_save(self)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(type(repo), "save", counting_save)
        lifecycle.record_comment_id("hlc-1", "10001")
        assert writes == [], "an identical re-record must not save"
        lifecycle.record_comment_id("hlc-2", "10002")
        assert writes == ["save"], "a genuinely new mapping must save exactly once"

    assert (_bridge(tmp_path) / "bindings.json").read_bytes() != before
    assert lifecycle.comment_id_for("hlc-1") == "10001"
    assert lifecycle.comment_id_for("hlc-2") == "10002"


def test_a_differing_id_for_a_known_key_overwrites_and_saves(tmp_path: Path) -> None:
    """Characterized, NOT idealized: a DIFFERENT id for a known key OVERWRITES it.

    The change-gate is equality-only — it short-circuits when the id matches and otherwise
    assigns and saves. Verified against the pre-extraction code at 4758d47f82: identical
    logic, so this extraction preserves it.

    The docstring on the reviewed base claimed "a key is never remapped to a different id",
    which the code does not do. That sentence has been corrected in the moved docstring
    rather than carried forward as a false statement, and no behavior was changed: making
    the map genuinely append-only would be a behavior change needing its own ticket, and
    an overwrite is plausibly wanted when a mirrored comment is recreated.
    """
    lifecycle, _ = _lifecycle(tmp_path)
    lifecycle.record_comment_id("hlc-1", "10001")

    lifecycle.record_comment_id("hlc-1", "99999")

    assert lifecycle.comment_id_for("hlc-1") == "99999"
    raw = json.loads((_bridge(tmp_path) / "bindings.json").read_text(encoding="utf-8"))
    assert raw["comment_ids"]["hlc-1"] == "99999", "the overwrite is persisted"


def test_comment_ids_are_coerced_to_strings(tmp_path: Path) -> None:
    """Jira returns numeric ids in some payload shapes; the map is keyed and valued as
    strings so a re-record with the other type is still recognized as identical."""
    lifecycle, _ = _lifecycle(tmp_path)

    lifecycle.record_comment_id(12345, 67890)  # type: ignore[arg-type]

    assert lifecycle.comment_id_for("12345") == "67890"
    assert lifecycle.is_comment_mapped("12345") is True


def test_unmapped_comment_key_reads_as_none(tmp_path: Path) -> None:
    lifecycle, _ = _lifecycle(tmp_path)

    assert lifecycle.comment_id_for("hlc-absent") is None
    assert lifecycle.is_comment_mapped("hlc-absent") is False


def test_legacy_store_without_the_comment_map_is_readable_without_rewrite(
    tmp_path: Path,
) -> None:
    """Old records predate the map entirely. Reading must not materialize it — an eager
    rewrite would touch every store on upgrade."""
    bridge = _bridge(tmp_path)
    bridge.mkdir(parents=True)
    legacy = {"version": 2, "bindings": {}, "reverse": {}}
    (bridge / "bindings.json").write_text(json.dumps(legacy), encoding="utf-8")
    before = (bridge / "bindings.json").read_bytes()

    lifecycle, _ = _lifecycle(tmp_path)

    assert lifecycle.comment_id_for("hlc-1") is None
    assert lifecycle.is_comment_mapped("hlc-1") is False
    assert (bridge / "bindings.json").read_bytes() == before, "a read must not rewrite"

    lifecycle.record_comment_id("hlc-1", "10001")
    raw = json.loads((bridge / "bindings.json").read_text(encoding="utf-8"))
    assert raw["comment_ids"] == {"hlc-1": "10001"}, "the map is created on first write"


# -- the fail-open / fail-closed asymmetry survives the move ---------------


def test_corrupt_retired_file_fails_open_and_leaves_live_state_usable(
    tmp_path: Path,
) -> None:
    """FAIL OPEN, in deliberate contrast to live state: a retired binding wrongly treated
    as live costs one wasted GET (it re-404s and re-retires), never a duplicate write."""
    bridge = _bridge(tmp_path)
    bridge.mkdir(parents=True)
    (bridge / "bindings.json").write_text(
        json.dumps(
            {
                "version": 2,
                "bindings": {"loc-A": {"jira_key": "DIG-A"}},
                "reverse": {"DIG-A": "loc-A"},
            }
        ),
        encoding="utf-8",
    )
    raw = "{corrupt retired"
    (bridge / "bindings-retired.json").write_text(raw, encoding="utf-8")

    lifecycle, repo = _lifecycle(tmp_path)

    assert lifecycle.is_retired("DIG-A") is False
    assert lifecycle.is_retired_local("loc-A") is False
    assert repo.bindings["loc-A"]["jira_key"] == "DIG-A", "live state stays usable"
    assert (bridge / "bindings-retired.json").read_text(encoding="utf-8") == raw


def test_corrupt_live_state_still_fails_closed(tmp_path: Path) -> None:
    """FAIL CLOSED. Degrading an unparseable live store to empty would report every ticket
    unbound and mass-duplicate them in Jira — irreversible, where aborting is not."""
    bridge = _bridge(tmp_path)
    bridge.mkdir(parents=True)
    (bridge / "bindings.json").write_text('{"bindings": {<<<<<<< HEAD\n', encoding="utf-8")

    with pytest.raises(ValueError, match="corrupt or contains git conflict"):
        _lifecycle(tmp_path)


def test_an_alert_write_failure_does_not_break_a_retirement(tmp_path: Path) -> None:
    """Alerting is observability and must never break a sync pass. With the alert store's
    parent path occupied by a regular file every append fails, and the retirement must
    still complete and persist."""
    lifecycle, _ = _bind_confirmed(tmp_path)
    (tmp_path / "bridge_state").write_text("not a directory", encoding="utf-8")

    for _ in range(3):
        lifecycle.note_absent("DIG-A")

    assert lifecycle.is_retired("DIG-A") is True
    assert _retired_file(tmp_path)["DIG-A"]["local_id"] == "loc-A"
    assert (tmp_path / "bridge_state").is_file()


def test_unknown_fields_on_a_retired_entry_survive_a_second_retirement(
    tmp_path: Path,
) -> None:
    """The retired file is rewritten wholesale, so fields another writer added to an
    existing tombstone must not be dropped when a new one is appended."""
    lifecycle, repo = _lifecycle(tmp_path)
    lifecycle.bind_confirm("loc-B", "DIG-B")
    repo.save_retired({"DIG-OLD": {"local_id": "loc-old", "future_field": {"keep": True}}})
    lifecycle2, _ = _lifecycle(tmp_path)
    lifecycle2.bind_confirm("loc-B", "DIG-B")

    for _ in range(3):
        lifecycle2.note_absent("DIG-B")

    retired = _retired_file(tmp_path)
    assert retired["DIG-OLD"]["future_field"] == {"keep": True}
    assert retired["DIG-B"]["local_id"] == "loc-B"
