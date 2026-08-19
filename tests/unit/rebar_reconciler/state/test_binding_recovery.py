"""Direct oracles for ``BindingRecovery`` — create recovery plus interrupted-retirement
completion.

RP-02 S3 T1 (``polarized-servile-jenny``). Two halves with DIFFERENT test obligations:

* **Create recovery is a MOVE.** Keyed/keyless pending recovery is lifted out of the
  facade unchanged, so its oracles are characterization: they must pass identically
  before and after. The pre-existing facade suites
  (``test_write_ahead_recovery.py``, ``test_recovery_loud_failures.py``,
  ``test_index_lag_duplicate_heldout.py``, ``test_identity_write_retention_heldout.py``)
  remain the regression oracle and are deliberately NOT duplicated here; what this file
  adds is the direct-module tier those facade tests cannot reach.
* **Retirement completion is NEW behavior.** So it is specified RED-first: every test
  below the create-recovery section describes an outcome that does not exist yet.

The state under repair is the one ordered partial state the retired-first protocol
produces by design (ADR 0099 §5): ``BindingLifecycle.retire`` persists the tombstone
through ``save_retired()`` and only then drops the live forward/reverse pair through
``save()``, so a crash between the two leaves ONE exact identity both live and
tombstoned. Every fixture here creates that state through the real production route —
``note_absent`` driven to the retirement grace with the live ``os.replace`` failing —
rather than by hand-writing an inconsistent pair of files.

Two behaviors are pinned here as they ACTUALLY are, against wording that implies
otherwise:

* A **corrupt live store** cannot produce a safety abort. It fails CLOSED inside
  ``BindingRepository._load``, so construction raises and repair is never reached. The
  safety property is real but it is delivered by fail-closed loading, not by an abort
  value.
* A **corrupt retired file** fails OPEN to an empty key set, which means a coherent
  tombstone becomes invisible and therefore yields no completion candidate at all. The
  live pair is preserved rather than deleted, which is the safe direction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from rebar_reconciler import binding_lifecycle, binding_recovery, binding_repository
from rebar_reconciler.binding_recovery import BindingRecovery
from rebar_reconciler.binding_repository import BindingRepository
from rebar_reconciler.binding_store import BindingStore

_INDENT = 2


# ---------------------------------------------------------------------------
# Fixtures: the REAL retired-first fault cut
# ---------------------------------------------------------------------------


def _canonical(payload: Any) -> bytes:
    return (json.dumps(payload, indent=_INDENT, sort_keys=True) + "\n").encode("utf-8")


def _bridge(root: Path) -> Path:
    return root / ".tickets-tracker" / ".bridge_state"


def _live_doc(
    *, pairs: dict[str, str] | None = None, extra_entry: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A production-shaped live store binding each ``local_id`` to its ``jira_key``."""
    pairs = pairs or {"loc-A": "DIG-A"}
    bindings: dict[str, Any] = {}
    reverse: dict[str, str] = {}
    for local_id, jira_key in pairs.items():
        entry: dict[str, Any] = {
            "jira_key": jira_key,
            "state": "confirmed",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        if extra_entry:
            entry.update(extra_entry)
        bindings[local_id] = entry
        reverse[jira_key] = local_id
    return {"version": 2, "bindings": bindings, "reverse": reverse, "comment_ids": {}}


def _seed(
    root: Path,
    *,
    doc: dict[str, Any] | None = None,
    retired: Any = None,
    raw_live: str | None = None,
    raw_retired: str | None = None,
) -> Path:
    bridge = _bridge(root)
    bridge.mkdir(parents=True, exist_ok=True)
    if raw_live is not None:
        (bridge / "bindings.json").write_text(raw_live, encoding="utf-8")
    elif doc is not None:
        (bridge / "bindings.json").write_bytes(_canonical(doc))
    if raw_retired is not None:
        (bridge / "bindings-retired.json").write_text(raw_retired, encoding="utf-8")
    elif retired is not None:
        (bridge / "bindings-retired.json").write_bytes(
            _canonical({"version": 1, "retired": retired})
        )
    return root / ".tickets-tracker"


def _fail_live_replace(mp: pytest.MonkeyPatch) -> None:
    """Fail ONLY the ``os.replace`` that commits ``bindings.json``.

    Every earlier write in the retirement sequence — crucially the tombstone — really
    lands, which is what makes this the true crash window rather than a simulation.
    """
    real = binding_repository.os.replace

    def fake(src: Any, dst: Any) -> Any:
        if Path(dst).name == "bindings.json":
            raise OSError("replace onto bindings.json failed")
        return real(src, dst)

    mp.setattr(binding_repository.os, "replace", fake)


def _make_overlap(
    root: Path,
    mp: pytest.MonkeyPatch,
    *,
    pairs: dict[str, str] | None = None,
    retire_key: str = "DIG-A",
) -> Path:
    """Produce the real live+tombstoned overlap on disk and return the tracker dir.

    Drives the PRODUCTION retirement route (``note_absent`` to the grace) with the live
    replacement failing, so the tombstone is durable and the live pair is not yet gone.
    """
    tracker = _seed(root, doc=_live_doc(pairs=pairs), retired={})
    store = BindingStore(tracker)
    live_before = (_bridge(root) / "bindings.json").read_bytes()
    _fail_live_replace(mp)
    with pytest.raises(OSError, match=r"replace onto bindings\.json failed"):
        for _ in range(int(binding_lifecycle._DEFAULT_ABSENT_RETIRE_GRACE)):
            store.note_absent(retire_key)
    mp.undo()
    # The fault cut must really have left the overlap, or every test below is vacuous.
    assert (_bridge(root) / "bindings.json").read_bytes() == live_before
    return tracker


def _recovery(tracker: Path) -> tuple[BindingRecovery, BindingRepository]:
    """A recovery owner over a FRESH read of the on-disk state."""
    repo = BindingRepository(tracker)
    lifecycle = binding_lifecycle.BindingLifecycle(repo)
    return BindingRecovery(repo, lifecycle), repo


def _live_on_disk(root: Path) -> dict[str, Any]:
    return json.loads((_bridge(root) / "bindings.json").read_text(encoding="utf-8"))


def _retired_on_disk(root: Path) -> dict[str, Any]:
    payload = json.loads((_bridge(root) / "bindings-retired.json").read_text(encoding="utf-8"))
    retired: dict[str, Any] = payload["retired"]
    return retired


class _SaveCounter:
    """Counts saves at the REPOSITORY layer, where the owners actually persist.

    Spying only on ``BindingStore.save`` is not enough: the policy owners hold the
    repository directly, so a save issued inside an owner never touches the facade
    method. That gap shipped a tautological no-save test once already in this epic
    (RP-02 S2 T3), so the spy lives at the lower layer deliberately.
    """

    def __init__(self, mp: pytest.MonkeyPatch) -> None:
        self.live = 0
        self.retired = 0
        real_save = BindingRepository.save
        real_save_retired = BindingRepository.save_retired

        def counted_save(inner: BindingRepository) -> None:
            self.live += 1
            real_save(inner)

        def counted_save_retired(inner: BindingRepository, entries: dict[str, Any]) -> None:
            self.retired += 1
            real_save_retired(inner, entries)

        mp.setattr(BindingRepository, "save", counted_save)
        mp.setattr(BindingRepository, "save_retired", counted_save_retired)


# ---------------------------------------------------------------------------
# The overlap fixture itself is load-bearing
# ---------------------------------------------------------------------------


def test_the_fault_cut_really_produces_a_live_and_tombstoned_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard on the fixture: without a genuine overlap every repair test is vacuous."""
    tracker = _make_overlap(tmp_path, monkeypatch)
    store = BindingStore(tracker)

    assert store.is_retired("DIG-A") is True
    assert store.is_bound("loc-A") is True
    assert store.get_jira_key("loc-A") == "DIG-A"
    assert store.get_local_id("DIG-A") == "loc-A"
    assert _retired_on_disk(tmp_path)["DIG-A"]["local_id"] == "loc-A"


# ---------------------------------------------------------------------------
# Completion: the exact-match happy path
# ---------------------------------------------------------------------------


def test_the_exact_overlap_completes_to_a_coherent_retired_only_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: tombstone, forward and reverse agree, so the redundant live pair
    is removed and the store is coherent on disk."""
    tracker = _make_overlap(tmp_path, monkeypatch)
    recovery, _repo = _recovery(tracker)

    outcome = recovery.complete_interrupted_retirements()

    assert [(r.jira_key, r.local_id) for r in outcome.completed] == [("DIG-A", "loc-A")]
    assert outcome.aborted == ()
    live = _live_on_disk(tmp_path)
    assert "loc-A" not in live["bindings"]
    assert "DIG-A" not in live["reverse"]


def test_completion_retains_the_tombstone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tombstone is the authoritative retirement INTENT, so completing the live side
    must never consume it — otherwise the soft delete becomes indistinguishable from a
    hard one, which is the incident the retired-first order exists to prevent."""
    tracker = _make_overlap(tmp_path, monkeypatch)
    before = _retired_on_disk(tmp_path)
    recovery, _repo = _recovery(tracker)

    recovery.complete_interrupted_retirements()

    assert _retired_on_disk(tmp_path) == before
    assert BindingStore(tracker).is_retired("DIG-A") is True


def test_completion_leaves_every_unrelated_binding_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repair is targeted: only the tombstoned identity's pair goes."""
    tracker = _make_overlap(tmp_path, monkeypatch, pairs={"loc-A": "DIG-A", "loc-B": "DIG-B"})
    recovery, _repo = _recovery(tracker)

    recovery.complete_interrupted_retirements()

    live = _live_on_disk(tmp_path)
    assert live["bindings"]["loc-B"]["jira_key"] == "DIG-B"
    assert live["reverse"] == {"DIG-B": "loc-B"}


def test_completion_pops_only_the_named_reverse_key_and_not_every_key_for_that_local(
    tmp_path: Path,
) -> None:
    """Removal of the reverse side is a TARGETED pop of the tombstoned key, never a sweep
    of every reverse key pointing at that local id.

    The discriminator is an out-of-band orphan: a reverse key the forward entry never
    named. A targeted pop leaves it; a sweep keyed on ``local_id`` takes it too, and
    nothing else in this file could tell those apart — every other fixture has exactly one
    reverse key per local id, which is precisely the blind spot that shipped a tautology
    earlier in this epic (RP-02 S2 T1).

    Pinning the orphan's SURVIVAL is deliberate, and it is characterization rather than an
    endorsement: a one-to-one forward/reverse invariant is not claimed anywhere, which is
    exactly why ``unbind`` carries an orphan sweep of its own and why bridge fsck reports
    ``reverse_missing_forward`` at all. Repair completes a retirement; it does not
    opportunistically tidy an index it was not asked about.
    """
    doc = _live_doc(pairs={"loc-A": "DIG-A"})
    doc["reverse"]["DIG-ORPHAN"] = "loc-A"
    tracker = _seed(
        tmp_path,
        doc=doc,
        retired={"DIG-A": {"local_id": "loc-A", "retired_at": "2026-01-02T00:00:00Z"}},
    )
    recovery, _repo = _recovery(tracker)

    outcome = recovery.complete_interrupted_retirements()

    assert [r.jira_key for r in outcome.completed] == ["DIG-A"]
    reverse = _live_on_disk(tmp_path)["reverse"]
    assert "DIG-A" not in reverse
    assert reverse["DIG-ORPHAN"] == "loc-A"


def test_a_reloaded_store_sees_the_repaired_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repair is durable, not just in-memory."""
    tracker = _make_overlap(tmp_path, monkeypatch)
    recovery, _repo = _recovery(tracker)

    recovery.complete_interrupted_retirements()

    reloaded = BindingStore(tracker)
    assert reloaded.is_bound("loc-A") is False
    assert reloaded.get_local_id("DIG-A") is None
    assert reloaded.is_retired("DIG-A") is True
    assert reloaded.retired_key_for_local("loc-A") == "DIG-A"


def test_a_coherent_tombstone_with_no_live_residue_is_not_a_candidate(
    tmp_path: Path,
) -> None:
    """The ordinary steady state. Almost every tombstone in a real store is already
    coherent, so it must produce neither a completion nor an abort — silence."""
    tracker = _seed(
        tmp_path,
        doc=_live_doc(pairs={"loc-B": "DIG-B"}),
        retired={"DIG-A": {"local_id": "loc-A", "retired_at": "2026-01-02T00:00:00Z"}},
    )
    recovery, _repo = _recovery(tracker)

    outcome = recovery.complete_interrupted_retirements()

    assert outcome.completed == ()
    assert outcome.aborted == ()


def test_a_store_with_no_tombstones_completes_nothing(tmp_path: Path) -> None:
    tracker = _seed(tmp_path, doc=_live_doc(), retired={})
    recovery, _repo = _recovery(tracker)

    outcome = recovery.complete_interrupted_retirements()

    assert (outcome.completed, outcome.aborted) == ((), ())


# ---------------------------------------------------------------------------
# The classifier is PURE
# ---------------------------------------------------------------------------


def test_the_classifier_mutates_nothing_it_is_given(tmp_path: Path) -> None:
    """Classification must be safe to run anywhere, including on a read-only pass, so it
    is a pure function over the three maps and never writes through them."""
    bindings = {"loc-A": {"jira_key": "DIG-A", "state": "confirmed"}}
    reverse = {"DIG-A": "loc-A"}
    retired = {"DIG-A": {"local_id": "loc-A"}}
    snapshot = json.dumps([bindings, reverse, retired], sort_keys=True)

    binding_recovery.classify_interrupted_retirements(bindings, reverse, retired)

    assert json.dumps([bindings, reverse, retired], sort_keys=True) == snapshot


def test_the_classifier_finds_the_exact_overlap() -> None:
    outcome = binding_recovery.classify_interrupted_retirements(
        {"loc-A": {"jira_key": "DIG-A"}}, {"DIG-A": "loc-A"}, {"DIG-A": {"local_id": "loc-A"}}
    )

    assert [(r.jira_key, r.local_id) for r in outcome.completed] == [("DIG-A", "loc-A")]
    assert outcome.aborted == ()


# ---------------------------------------------------------------------------
# Idempotency — the WRITE-COUNTING oracle
# ---------------------------------------------------------------------------


def test_repeating_completion_performs_no_further_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Repeat is a no-op" has to be measured in WRITES, not in bytes: the store
    serializes deterministically, so an unconditional re-save produces byte-identical
    output and a bytes-comparison oracle could never fail."""
    tracker = _make_overlap(tmp_path, monkeypatch)
    recovery, _repo = _recovery(tracker)
    recovery.complete_interrupted_retirements()

    counter = _SaveCounter(monkeypatch)
    second = recovery.complete_interrupted_retirements()

    assert (second.completed, second.aborted) == ((), ())
    assert (counter.live, counter.retired) == (0, 0)


def test_a_freshly_reloaded_store_also_finds_nothing_to_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotency across a process boundary, not just within one owner's memory."""
    tracker = _make_overlap(tmp_path, monkeypatch)
    first, _repo = _recovery(tracker)
    first.complete_interrupted_retirements()

    counter = _SaveCounter(monkeypatch)
    second, _repo2 = _recovery(tracker)
    outcome = second.complete_interrupted_retirements()

    assert (outcome.completed, outcome.aborted) == ((), ())
    assert (counter.live, counter.retired) == (0, 0)


def test_a_nothing_to_do_call_never_touches_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overwhelmingly common case — a healthy store — must be free."""
    tracker = _seed(tmp_path, doc=_live_doc(), retired={})
    recovery, _repo = _recovery(tracker)
    counter = _SaveCounter(monkeypatch)

    recovery.complete_interrupted_retirements()

    assert (counter.live, counter.retired) == (0, 0)


def test_completion_saves_the_live_store_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One repair, one live write, and NO retired write — the tombstone is already
    durable, so rewriting it would risk the one file retirement's reversibility rests on
    for no gain."""
    tracker = _make_overlap(tmp_path, monkeypatch, pairs={"loc-A": "DIG-A", "loc-B": "DIG-B"})
    recovery, _repo = _recovery(tracker)
    counter = _SaveCounter(monkeypatch)

    recovery.complete_interrupted_retirements()

    assert (counter.live, counter.retired) == (1, 0)


# ---------------------------------------------------------------------------
# The mismatch table — zero guessed deletion
# ---------------------------------------------------------------------------


def test_a_forward_entry_naming_a_different_key_aborts_without_deleting(
    tmp_path: Path,
) -> None:
    """The local ticket has moved on to another issue. Deleting its live pair on the
    strength of an older tombstone would unbind a HEALTHY binding."""
    tracker = _seed(
        tmp_path,
        doc=_live_doc(pairs={"loc-A": "DIG-OTHER"}),
        retired={"DIG-A": {"local_id": "loc-A", "retired_at": "2026-01-02T00:00:00Z"}},
    )
    recovery, _repo = _recovery(tracker)

    outcome = recovery.complete_interrupted_retirements()

    assert outcome.completed == ()
    assert [a.reason for a in outcome.aborted] == [binding_recovery.ABORT_FORWARD_KEY_MISMATCH]
    live = _live_on_disk(tmp_path)
    assert live["bindings"]["loc-A"]["jira_key"] == "DIG-OTHER"
    assert live["reverse"] == {"DIG-OTHER": "loc-A"}


def test_a_reverse_entry_naming_a_different_local_aborts_without_deleting(
    tmp_path: Path,
) -> None:
    """The reverse index points somewhere else, so the two sides do not agree on who
    owns the key and there is no safe single answer."""
    doc = _live_doc(pairs={"loc-A": "DIG-A"})
    doc["reverse"]["DIG-A"] = "loc-STRANGER"
    tracker = _seed(
        tmp_path,
        doc=doc,
        retired={"DIG-A": {"local_id": "loc-A", "retired_at": "2026-01-02T00:00:00Z"}},
    )
    recovery, _repo = _recovery(tracker)

    outcome = recovery.complete_interrupted_retirements()

    assert outcome.completed == ()
    assert [a.reason for a in outcome.aborted] == [binding_recovery.ABORT_REVERSE_MISMATCH]
    assert _live_on_disk(tmp_path)["reverse"] == {"DIG-A": "loc-STRANGER"}
    assert "loc-A" in _live_on_disk(tmp_path)["bindings"]


def test_a_missing_reverse_entry_aborts_without_deleting_the_forward(
    tmp_path: Path,
) -> None:
    """Half the pair is gone. That is a DIFFERENT partial state from the one the
    retired-first order produces, so completion has no warrant to finish it."""
    doc = _live_doc(pairs={"loc-A": "DIG-A"})
    doc["reverse"] = {}
    tracker = _seed(
        tmp_path,
        doc=doc,
        retired={"DIG-A": {"local_id": "loc-A", "retired_at": "2026-01-02T00:00:00Z"}},
    )
    recovery, _repo = _recovery(tracker)

    outcome = recovery.complete_interrupted_retirements()

    assert outcome.completed == ()
    assert [a.reason for a in outcome.aborted] == [binding_recovery.ABORT_REVERSE_MISSING]
    assert "loc-A" in _live_on_disk(tmp_path)["bindings"]


def test_a_missing_forward_entry_aborts_without_deleting_the_reverse(
    tmp_path: Path,
) -> None:
    """The mirror case: a dangling reverse key with no forward entry."""
    doc = _live_doc(pairs={"loc-B": "DIG-B"})
    doc["reverse"]["DIG-A"] = "loc-A"
    tracker = _seed(
        tmp_path,
        doc=doc,
        retired={"DIG-A": {"local_id": "loc-A", "retired_at": "2026-01-02T00:00:00Z"}},
    )
    recovery, _repo = _recovery(tracker)

    outcome = recovery.complete_interrupted_retirements()

    assert outcome.completed == ()
    assert [a.reason for a in outcome.aborted] == [binding_recovery.ABORT_FORWARD_MISSING]
    assert _live_on_disk(tmp_path)["reverse"]["DIG-A"] == "loc-A"


def test_a_tombstone_without_a_usable_local_id_aborts(tmp_path: Path) -> None:
    """A legacy or hand-edited tombstone carrying no ``local_id`` names no forward entry,
    so nothing can be matched and nothing may be guessed."""
    doc = _live_doc(pairs={"loc-A": "DIG-A"})
    tracker = _seed(tmp_path, doc=doc, retired={"DIG-A": {"retired_at": "2026-01-02T00:00:00Z"}})
    recovery, _repo = _recovery(tracker)

    outcome = recovery.complete_interrupted_retirements()

    assert outcome.completed == ()
    assert [a.reason for a in outcome.aborted] == [binding_recovery.ABORT_TOMBSTONE_LOCAL_MISSING]
    assert "loc-A" in _live_on_disk(tmp_path)["bindings"]


def test_a_malformed_live_entry_shape_aborts_without_deleting(tmp_path: Path) -> None:
    """Not every bad live store is unparseable JSON. A forward entry that is not a dict
    parses fine and reaches the classifier, and THAT is the reachable corrupt-live case:
    an unparseable file never gets this far, because the repository fails closed at load.
    """
    doc = _live_doc(pairs={"loc-A": "DIG-A"})
    doc["bindings"]["loc-A"] = "not-a-mapping"
    tracker = _seed(
        tmp_path,
        doc=doc,
        retired={"DIG-A": {"local_id": "loc-A", "retired_at": "2026-01-02T00:00:00Z"}},
    )
    recovery, _repo = _recovery(tracker)

    outcome = recovery.complete_interrupted_retirements()

    assert outcome.completed == ()
    assert [a.reason for a in outcome.aborted] == [binding_recovery.ABORT_MALFORMED_ENTRY]
    assert _live_on_disk(tmp_path)["bindings"]["loc-A"] == "not-a-mapping"


def test_an_abort_carries_the_evidence_a_human_needs(tmp_path: Path) -> None:
    """A safety abort is a REPORT, not a shrug: whoever reads it must be able to see
    which identities disagreed without re-deriving the state by hand."""
    tracker = _seed(
        tmp_path,
        doc=_live_doc(pairs={"loc-A": "DIG-OTHER"}),
        retired={"DIG-A": {"local_id": "loc-A", "retired_at": "2026-01-02T00:00:00Z"}},
    )
    recovery, _repo = _recovery(tracker)

    (abort,) = recovery.complete_interrupted_retirements().aborted

    assert abort.jira_key == "DIG-A"
    rendered = json.dumps(abort.evidence, sort_keys=True)
    assert "loc-A" in rendered
    assert "DIG-OTHER" in rendered


def test_an_abort_on_one_tombstone_does_not_block_completing_another(
    tmp_path: Path,
) -> None:
    """Per-tombstone isolation. One unsafe candidate must not strand a safe one, exactly
    as one failed create-recovery entry does not stop the others."""
    doc = _live_doc(pairs={"loc-A": "DIG-A", "loc-B": "DIG-OTHER"})
    tracker = _seed(
        tmp_path,
        doc=doc,
        retired={
            "DIG-A": {"local_id": "loc-A", "retired_at": "2026-01-02T00:00:00Z"},
            "DIG-B": {"local_id": "loc-B", "retired_at": "2026-01-02T00:00:00Z"},
        },
    )
    recovery, _repo = _recovery(tracker)

    outcome = recovery.complete_interrupted_retirements()

    assert [r.jira_key for r in outcome.completed] == ["DIG-A"]
    assert [a.reason for a in outcome.aborted] == [binding_recovery.ABORT_FORWARD_KEY_MISMATCH]
    live = _live_on_disk(tmp_path)
    assert "loc-A" not in live["bindings"]
    assert live["bindings"]["loc-B"]["jira_key"] == "DIG-OTHER"


# ---------------------------------------------------------------------------
# Replace failure — retain the overlap, abort for a later retry
# ---------------------------------------------------------------------------


def test_a_live_replace_failure_retains_the_overlap_and_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the repair's own write cannot land, the state must be left exactly as it was so
    a later pass can retry. Losing the overlap here would lose the evidence."""
    tracker = _make_overlap(tmp_path, monkeypatch)
    live_before = (_bridge(tmp_path) / "bindings.json").read_bytes()
    recovery, _repo = _recovery(tracker)
    _fail_live_replace(monkeypatch)

    outcome = recovery.complete_interrupted_retirements()

    assert outcome.completed == ()
    (abort,) = outcome.aborted
    assert abort.reason == binding_recovery.ABORT_REPLACE_FAILED
    # The underlying fault is carried in the evidence, not just its category: "the write
    # failed" is not actionable, "replace onto bindings.json failed" names the file and
    # the syscall an operator has to go and look at.
    assert "replace onto bindings.json failed" in abort.evidence["error"]
    assert (_bridge(tmp_path) / "bindings.json").read_bytes() == live_before
    assert _retired_on_disk(tmp_path)["DIG-A"]["local_id"] == "loc-A"


def test_the_overlap_is_restored_in_memory_after_a_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-memory view must not drift away from disk on a failed repair: the rest of
    the pass keeps reading these dictionaries, and a phantom deletion there would make
    the pass act on a state that was never persisted."""
    tracker = _make_overlap(tmp_path, monkeypatch)
    recovery, repo = _recovery(tracker)
    _fail_live_replace(monkeypatch)

    recovery.complete_interrupted_retirements()

    assert repo.bindings["loc-A"]["jira_key"] == "DIG-A"
    assert repo.reverse["DIG-A"] == "loc-A"


def test_a_replace_failure_leaves_no_temp_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = _make_overlap(tmp_path, monkeypatch)
    recovery, _repo = _recovery(tracker)
    _fail_live_replace(monkeypatch)

    recovery.complete_interrupted_retirements()

    leftovers = [p.name for p in _bridge(tmp_path).iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Corrupt state: pinned as it really behaves
# ---------------------------------------------------------------------------


def test_a_corrupt_live_store_fails_closed_before_repair_can_run(tmp_path: Path) -> None:
    """An unparseable live store never reaches repair — the repository raises at load.
    Fail-closed loading, not an abort value, is what protects this case; degrading to an
    empty store is what mass-duplicates Jira issues."""
    tracker = _seed(
        tmp_path,
        raw_live="{ corrupt <<<<<<< HEAD",
        retired={"DIG-A": {"local_id": "loc-A"}},
    )

    with pytest.raises(ValueError, match=r"bindings\.json is corrupt"):
        _recovery(tracker)


def test_a_corrupt_retired_file_yields_no_candidate_and_keeps_the_live_pair(
    tmp_path: Path,
) -> None:
    """The retired file fails OPEN, so a corrupt one makes tombstones invisible. The
    consequence must be a MISSED repair, never a guessed deletion: no tombstone means no
    authoritative intent, and the live pair stays."""
    tracker = _seed(tmp_path, doc=_live_doc(pairs={"loc-A": "DIG-A"}), raw_retired="{corrupt")
    recovery, _repo = _recovery(tracker)

    outcome = recovery.complete_interrupted_retirements()

    assert (outcome.completed, outcome.aborted) == ((), ())
    assert _live_on_disk(tmp_path)["bindings"]["loc-A"]["jira_key"] == "DIG-A"


# ---------------------------------------------------------------------------
# Tombstone authority vs. automatic liveness signals
# ---------------------------------------------------------------------------


def test_an_explicit_unretire_removes_the_completion_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``unretire`` is the ONLY documented revocation of retirement intent. Once the
    tombstone is lifted the overlap is no longer an overlap — it is an ordinary live
    binding — so repair must not touch it."""
    tracker = _make_overlap(tmp_path, monkeypatch)
    store = BindingStore(tracker)
    assert store.unretire("DIG-A") is True

    recovery, _repo = _recovery(tracker)
    outcome = recovery.complete_interrupted_retirements()

    assert (outcome.completed, outcome.aborted) == ((), ())
    assert _live_on_disk(tmp_path)["bindings"]["loc-A"]["jira_key"] == "DIG-A"


def test_a_same_key_confirm_does_not_revoke_the_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A historical overlap whose live record was re-confirmed to the SAME key is still a
    completion candidate. An automatic confirm is not a revocation signal; treating it as
    one would resurrect an identity an operator deliberately retired."""
    tracker = _make_overlap(tmp_path, monkeypatch)
    store = BindingStore(tracker)
    store.bind_confirm("loc-A", "DIG-A")
    store.save()

    recovery, _repo = _recovery(tracker)
    outcome = recovery.complete_interrupted_retirements()

    assert [r.jira_key for r in outcome.completed] == ["DIG-A"]
    assert BindingStore(tracker).is_retired("DIG-A") is True


def test_clear_absent_does_not_revoke_the_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later 200 clears the absence counter, and that is all it does. The counter is
    evidence about liveness; the tombstone is a decision, and evidence does not overturn
    a decision automatically."""
    tracker = _make_overlap(tmp_path, monkeypatch)
    store = BindingStore(tracker)
    store.clear_absent("DIG-A")
    store.save()

    recovery, _repo = _recovery(tracker)
    outcome = recovery.complete_interrupted_retirements()

    assert [r.jira_key for r in outcome.completed] == ["DIG-A"]
    assert BindingStore(tracker).is_retired("DIG-A") is True


# ---------------------------------------------------------------------------
# Create recovery: the extraction's direct tier
# ---------------------------------------------------------------------------


class _FakeClient:
    """Records the identity writes and searches create recovery performs."""

    def __init__(self, results: list[dict[str, str]] | None = None) -> None:
        self.results = results or []
        self.searches: list[str] = []
        self.labels: list[tuple[str, str]] = []
        self.properties: list[tuple[str, str, str]] = []

    def search_issues(self, jql: str) -> list[dict[str, str]]:
        self.searches.append(jql)
        return self.results

    def add_label(self, key: str, label: str) -> None:
        self.labels.append((key, label))

    def set_entity_property(self, key: str, name: str, value: str) -> None:
        self.properties.append((key, name, value))


def _pending_tracker(tmp_path: Path, *, entry: dict[str, Any], local_id: str = "loc-P") -> Path:
    doc: dict[str, Any] = {
        "version": 2,
        "bindings": {local_id: entry},
        "reverse": {},
        "comment_ids": {},
    }
    return _seed(tmp_path, doc=doc, retired={})


def test_keyed_pending_recovery_performs_no_search(tmp_path: Path) -> None:
    """The write-ahead recorded the key BEFORE the label went on, so the create is known
    to have landed. Searching would risk a duplicate for no information."""
    tracker = _pending_tracker(
        tmp_path,
        entry={"state": "pending", "jira_key": "DIG-P", "created_at": "2026-01-01T00:00:00Z"},
    )
    recovery, repo = _recovery(tracker)
    client = _FakeClient()

    resolved = recovery.recover_pending_bindings(client)

    assert resolved == 1
    assert client.searches == []
    assert client.labels == [("DIG-P", "rebar-id:loc-P")]
    assert repo.bindings["loc-P"]["state"] == "confirmed"


def test_keyless_pending_confirms_on_the_colon_form_label(tmp_path: Path) -> None:
    tracker = _pending_tracker(
        tmp_path, entry={"state": "pending", "created_at": "2026-01-01T00:00:00Z"}
    )
    recovery, repo = _recovery(tracker)
    client = _FakeClient(results=[{"key": "DIG-FOUND"}])

    resolved = recovery.recover_pending_bindings(client)

    assert resolved == 1
    assert client.searches == ['labels = "rebar-id:loc-P"']
    assert repo.bindings["loc-P"]["jira_key"] == "DIG-FOUND"


def test_a_single_keyless_miss_does_not_unbind(tmp_path: Path) -> None:
    """A negative search is not proof of absence on Jira DC, whose index lags. Unbinding
    on one miss is what creates duplicates."""
    tracker = _pending_tracker(
        tmp_path, entry={"state": "pending", "created_at": "2026-01-01T00:00:00Z"}
    )
    recovery, repo = _recovery(tracker)

    resolved = recovery.recover_pending_bindings(_FakeClient(results=[]))

    assert resolved == 0
    assert repo.bindings["loc-P"]["state"] == "pending"
    assert repo.bindings["loc-P"]["search_miss_count"] == 1


def test_keyless_unbind_needs_both_three_misses_and_the_grace_window(
    tmp_path: Path,
) -> None:
    """The conjunction is the contract: corroborated absence AND an entry too old for the
    documented index lag to explain. Either alone is not enough."""
    tracker = _pending_tracker(
        tmp_path,
        entry={
            "state": "pending",
            "created_at": "2020-01-01T00:00:00Z",
            "search_miss_count": 2,
        },
    )
    recovery, repo = _recovery(tracker)

    resolved = recovery.recover_pending_bindings(_FakeClient(results=[]))

    assert resolved == 1
    assert "loc-P" not in repo.bindings


def test_an_old_entry_with_too_few_misses_stays_pending(tmp_path: Path) -> None:
    tracker = _pending_tracker(
        tmp_path,
        entry={
            "state": "pending",
            "created_at": "2020-01-01T00:00:00Z",
            "search_miss_count": 0,
        },
    )
    recovery, repo = _recovery(tracker)

    assert recovery.recover_pending_bindings(_FakeClient(results=[])) == 0
    assert repo.bindings["loc-P"]["state"] == "pending"


def test_a_young_entry_is_not_unbound_however_many_misses(tmp_path: Path) -> None:
    tracker = _pending_tracker(
        tmp_path,
        entry={
            "state": "pending",
            "created_at": binding_recovery._now_iso(),
            "search_miss_count": 99,
        },
    )
    recovery, repo = _recovery(tracker)

    assert recovery.recover_pending_bindings(_FakeClient(results=[])) == 0
    assert repo.bindings["loc-P"]["state"] == "pending"


def test_a_per_entry_failure_is_recorded_and_the_others_continue(tmp_path: Path) -> None:
    """Loud but non-fatal: a broken entry must not strand every other pending binding."""
    doc: dict[str, Any] = {
        "version": 2,
        "bindings": {
            "loc-BAD": {
                "state": "pending",
                "jira_key": "DIG-BAD",
                "created_at": "2026-01-01T00:00:00Z",
            },
            "loc-OK": {
                "state": "pending",
                "jira_key": "DIG-OK",
                "created_at": "2026-01-01T00:00:00Z",
            },
        },
        "reverse": {},
        "comment_ids": {},
    }
    tracker = _seed(tmp_path, doc=doc, retired={})
    recovery, repo = _recovery(tracker)

    class _Selective(_FakeClient):
        def add_label(self, key: str, label: str) -> None:
            if key == "DIG-BAD":
                raise RuntimeError("label write refused")
            super().add_label(key, label)

    sink: list[dict[str, Any]] = []
    resolved = recovery.recover_pending_bindings(_Selective(), failure_sink=sink)

    assert resolved == 1
    assert [f["local_id"] for f in sink] == ["loc-BAD"]
    assert repo.bindings["loc-OK"]["state"] == "confirmed"
    assert repo.bindings["loc-BAD"]["state"] == "pending"


def test_create_recovery_carries_no_write_bearing_or_scope_parameter(tmp_path: Path) -> None:
    """AC6 of this task, as a contract rather than prose: moving create recovery must not
    attach a new gate to it. Its invocation condition stays entirely in the caller — the
    single ``if not scoped_ids`` guard in ``run_differs.py``, which this change does not
    touch — so the recovery owner takes no ``persist``/``dry_run``/scope argument."""
    import inspect

    params = list(inspect.signature(BindingRecovery.recover_pending_bindings).parameters)

    assert params == ["self", "client", "failure_sink"]


# ---------------------------------------------------------------------------
# The facade stays compatible
# ---------------------------------------------------------------------------


def test_the_facade_delegates_create_recovery_unchanged(tmp_path: Path) -> None:
    """The public method keeps its name, keyword and return contract, so the existing
    ``run_differs.py`` call site needs no edit at all."""
    tracker = _pending_tracker(
        tmp_path,
        entry={"state": "pending", "jira_key": "DIG-P", "created_at": "2026-01-01T00:00:00Z"},
    )
    store = BindingStore(tracker)
    sink: list[dict[str, Any]] = []

    resolved = store.recover_pending_bindings(_FakeClient(), failure_sink=sink)

    assert resolved == 1
    assert sink == []
    assert store.all_bindings()["loc-P"]["state"] == "confirmed"


def test_the_facade_exposes_retirement_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The facade stays the only public door: callers reach completion through it, never
    through the recovery owner."""
    tracker = _make_overlap(tmp_path, monkeypatch)
    store = BindingStore(tracker)

    outcome = store.complete_interrupted_retirements()

    assert [r.jira_key for r in outcome.completed] == ["DIG-A"]
    assert store.is_bound("loc-A") is False


def test_the_facade_grace_names_are_aliases_and_not_a_second_source_of_truth() -> None:
    """The facade re-exports the index-lag grace, the miss threshold and the age helper.

    They must be the SAME objects the recovery owner enforces, not copies. A copy would
    let the two drift apart silently: ``is_keyless_pending_within_grace`` would answer
    with the facade's value while the unbind conjunction used the owner's, so a store
    could suppress a create and unbind the same binding in one pass. Identity is the
    cheapest oracle for that, and it is what makes the "read-only labels" comment beside
    the aliases enforceable rather than a request.
    """
    import rebar_reconciler.binding_store as bs

    assert bs._INDEX_LAG_GRACE_SECONDS is binding_recovery._INDEX_LAG_GRACE_SECONDS
    assert bs._MISSES_BEFORE_UNBIND is binding_recovery._MISSES_BEFORE_UNBIND
    assert bs._age_seconds is binding_recovery._age_seconds


def test_the_recovery_owner_is_not_handed_out_by_the_facade(tmp_path: Path) -> None:
    """Same rule the repository and lifecycle owners follow: no public attribute or
    method returns an owner, or a caller could write binding state without passing
    through the facade's coordination."""
    tracker = _seed(tmp_path, doc=_live_doc(), retired={})
    store = BindingStore(tracker)

    public = [
        name
        for name in dir(store)
        if not name.startswith("_") and isinstance(getattr(store, name, None), BindingRecovery)
    ]

    assert public == []


def test_the_module_imports_in_isolation() -> None:
    """The reconciler test tree loads these modules by file path, so the recovery module
    must not acquire an import cycle back to the facade."""
    assert binding_recovery.__name__ in sys.modules
    assert "binding_recovery" in binding_recovery.__file__
