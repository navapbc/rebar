"""BindingStore coordinates Jira binding state over one repository.

BindingRepository owns the binding file, retired and rotation sidecars, and alert log.
BindingLifecycle owns identity, absence, retirement, and comment transitions.
BindingRecovery repairs pending creates and interrupted retirement. These owners share
repository dictionaries, and save() is the persistence boundary.

Create persists a pending entry before calling Jira. Once Jira returns a key, it
persists that key before attaching the rebar-id label and property. Recovery can
confirm a keyed pending entry without searching Jira. A keyless entry requires a
label search. Per-entry recovery failures leave the entry pending and reach the
failure sink. The comment_ids map records local keys to Jira IDs for idempotent
comment sync."""

from __future__ import annotations

# Keep binding_store.os as a patch point for atomic-write crash tests of os.replace.
import os  # noqa: F401
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._backend import TicketTransport

from rebar.reconciler import binding_lifecycle, binding_recovery, get_rotation, peer_state
from rebar.reconciler.binding_repository import BindingRepository
from rebar.reconciler.timeutil import utc_now_iso


class BindingPersistError(RuntimeError):
    """A write-ahead binding persist (``save()``) failed before ``create_issue``.

    Raised by the outbound-create write-ahead path (dispatch_one) when the
    durable pending record cannot be persisted. A durable pending record is a
    PRECONDITION for a safe create (it is what recovery keys on), so the create
    is skipped rather than run without one — the mutation is recorded failed and
    an alert fires, and the pass continues with the remaining mutations.
    """


# These read-only aliases expose recovery policy to callers without a second source of truth.
_INDEX_LAG_GRACE_SECONDS = binding_recovery._INDEX_LAG_GRACE_SECONDS
_MISSES_BEFORE_UNBIND = binding_recovery._MISSES_BEFORE_UNBIND
_age_seconds = binding_recovery._age_seconds


def _now_iso() -> str:
    # Canonical Z-suffix UTC (twin of rebar.timeutils.utc_now_iso). RETAINED with no
    # caller left in this file: the last one moved to ``binding_recovery`` with pending
    # recovery, but this name is part of the module's PATCHABLE surface — the baseline and
    # idempotency oracles freeze time by ``monkeypatch.setattr`` on it, so deleting it
    # turns those suites into AttributeError rather than a behaviour change.
    return utc_now_iso()


# The retirement grace fallback comes from BindingLifecycle. Keep this inspection alias.
_DEFAULT_ABSENT_RETIRE_GRACE = binding_lifecycle._DEFAULT_ABSENT_RETIRE_GRACE


def _current_key_by_id(client: TicketTransport, jira_id: str) -> str:
    """Read the current issue key by immutable Jira ID through the primary REST lookup.
    Return an empty string when the method, lookup, or string key is unavailable, so
    absence bookkeeping continues."""
    fn = getattr(client, "get_issue_by_rest", None)
    if fn is None:
        return ""
    try:
        payload = fn(jira_id)
    except Exception:  # noqa: BLE001 — fail-closed to "no answer": absence bookkeeping proceeds
        return ""
    if not isinstance(payload, dict):
        return ""
    key = payload.get("key")
    return key if isinstance(key, str) else ""


class BindingStore:
    """Bidirectional local-id ↔ jira-key binding store.

    The FACADE, and the only public door to binding state. Three private owners sit behind
    it: :class:`BindingRepository` owns every byte on disk, ``BindingLifecycle`` owns
    lifecycle policy — the identity transitions (bind/confirm/unbind, the immutable numeric
    id, the MOVED-issue re-key) plus absence bookkeeping, retirement, tombstones and
    comment identity — and ``BindingRecovery`` owns repair of operations a crash left part
    way through. This class owns what is left — GET-rotation policy — and coordinates the
    three. Mutations are in-memory until ``save()``, which delegates the atomic write; the
    few operations that must be durable the instant they return (retirement, comment
    identity) persist themselves.
    """

    def __init__(self, tracker_dir: Path) -> None:
        """Load binding state without writing it. The facade aliases repository dictionaries
        so lifecycle, recovery, and peer-state changes share the same objects. The first save
        creates .bridge_state. A corrupt binding file raises. A corrupt retired sidecar
        becomes an empty set with an alert."""
        self._repo = BindingRepository(tracker_dir)
        self._lifecycle = binding_lifecycle.BindingLifecycle(self._repo)
        self._recovery = binding_recovery.BindingRecovery(self._repo, self._lifecycle)
        # Mirror repository paths for inspection. The repository owns every write.
        self._path = self._repo.path
        self._retired_path = self._repo.retired_path
        self._get_rotation_path = self._repo.rotation_path
        self._repo_root = self._repo.repo_root
        self._data = self._repo.data
        self._get_rotation = self._repo.rotation
        # Ticket 0fa2 (outbound-comment invariant): per-pass tallies. The store is
        # constructed once per reconcile pass, so "gained this pass" is measured
        # against the map size captured here at load.
        self._comment_posts = 0
        self._comment_ids_at_load = len(self._data.get("comment_ids", {}))

    # -- persistence (delegated to BindingRepository) -----------------------

    def save(self) -> None:
        """Persist the pass's state through the repository. UNCONDITIONAL.

        :meth:`BindingRepository.save` owns the ordering and the failure asymmetry: the
        GET-rotation sidecar is written FIRST and the legacy inline ``last_get_pass``
        floor is scrubbed from the live entries only once that write durably took it, so
        a fail-open rotation write never loses the floor and never aborts the save. The
        live replacement is atomic and fails CLOSED (raises) — a lost binding write is
        exactly what makes the next pass create duplicate Jira issues. No dirty-gating
        and no write elision: an unchanged store is still rewritten.
        """
        self._repo.save()

    # -- queries -----------------------------------------------------------

    def get_jira_key(self, local_id: str) -> str | None:
        entry = self._data["bindings"].get(local_id)
        if entry is None:
            return None
        return entry.get("jira_key")

    def get_local_id(self, jira_key: str) -> str | None:
        return self._data["reverse"].get(jira_key)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._data["bindings"]

    def is_pending(self, local_id: str) -> bool:
        entry = self._data["bindings"].get(local_id)
        return entry is not None and entry.get("state") == "pending"

    def is_keyless_pending_within_grace(self, local_id: str) -> bool:
        """Defer a new create while a keyless pending entry is within the index-lag grace.
        A negative Jira search cannot distinguish an absent issue from an unindexed create.
        Recovery confirms a later search hit. Expiration permits a later create. Keyed
        pending entries recover without search."""
        entry = self._data["bindings"].get(local_id)
        if entry is None or entry.get("state") != "pending" or entry.get("jira_key"):
            return False
        return _age_seconds(entry.get("created_at")) < _INDEX_LAG_GRACE_SECONDS

    def all_bindings(self) -> dict[str, dict]:
        """Return a new outer mapping with the repository's current entry dictionaries.
        Callers may iterate while bindings change, but inner entries remain shared. Named
        operations own writes. Callers must not mutate entries through this read query."""
        return dict(self._data["bindings"])

    def pending_bindings(self) -> list[str]:
        return [
            lid for lid, entry in self._data["bindings"].items() if entry.get("state") == "pending"
        ]

    def confirmed_count(self) -> int:
        return sum(
            1 for entry in self._data["bindings"].values() if entry.get("state") == "confirmed"
        )

    # Keep identity transitions on this facade while BindingLifecycle mutates the shared store.

    def bind_pending(self, local_id: str) -> None:
        """Mark a local ticket as pending outbound creation (write-ahead step 1)."""
        self._lifecycle.bind_pending(local_id)

    def record_pending_key(self, local_id: str, jira_key: str) -> None:
        """Record the Jira key on a STILL-pending entry (write-ahead step 3)."""
        self._lifecycle.record_pending_key(local_id, jira_key)

    def bind_confirm(self, local_id: str, jira_key: str) -> None:
        """Confirm binding after Jira issue creation succeeds (write-ahead step 5)."""
        self._lifecycle.bind_confirm(local_id, jira_key)

    def unbind(self, local_id: str) -> None:
        """Remove binding (for cleanup/rollback), clearing BOTH indexes."""
        self._lifecycle.unbind(local_id)

    # Last-synced PEER STATE thin delegates — semantics + unit tests: peer_state.py (4522).

    def get_baseline(self, local_id: str) -> dict[str, Any] | None:
        return peer_state.get_baseline(self._data["bindings"], local_id)

    def set_baseline(self, local_id: str, fields: dict[str, Any]) -> None:
        peer_state.set_baseline(self._data["bindings"], local_id, fields)

    def merge_baseline(self, local_id: str, fields: dict[str, Any]) -> None:
        return peer_state.merge_baseline(self._data["bindings"], local_id, fields)

    def get_peer_parent(self, local_id: str) -> str | None:
        return peer_state.get_peer_parent(self._data["bindings"], local_id)

    def set_peer_parent(self, local_id: str, parent_key: str | None) -> None:
        peer_state.set_peer_parent(self._data["bindings"], local_id, parent_key)

    def seed_baselines_from_snapshot(self, prev_snapshot: dict[str, Any]) -> int:
        return peer_state.seed_baselines_from_snapshot(self._data["bindings"], prev_snapshot)

    # -- immutable numeric id (bug 7c26) -----------------------------------

    def get_jira_id(self, local_id: str) -> str | None:
        """Return the immutable Jira ID, or None for a binding whose ID was never captured."""
        return self._lifecycle.get_jira_id(local_id)

    def record_jira_id(self, local_id: str, jira_id: str) -> None:
        """Capture the immutable numeric id on an EXISTING binding (bug 7c26).

        In-memory until :meth:`save` (the caller persists it with the same write that
        records the key). A no-op for an unbound local id, an empty id, or a re-record of
        the same value.
        """
        self._lifecycle.record_jira_id(local_id, jira_id)

    # -- rich-text emission state (morose-selfaware-unicorn) ---------------

    def note_rich_emit(self, local_id: str, wire: Any) -> int | None:
        """Record that ``wire`` was CONFIRMEDLY pushed as this binding's description.

        The facade's NAMED door onto rich-emission state, and the supported replacement
        for reaching a live entry through the shallow ``all_bindings()`` query and writing
        to it — a read-shaped call being used as an unowned write seam. Policy (and the
        rationale for every choice below) is ``BindingLifecycle.note_rich_emit``'s; this
        is a thin delegate, so the mature caller contract stays on ``BindingStore``.

        Returns the consecutive-identical-push count (0 on the first push of a wire), or
        ``None`` when the local id is unbound. A missing binding is nonfatal.

        ``None`` rather than ``0`` is defensive, and the hazard it guards is LATENT rather
        than present: the caller compares against an equality threshold that is currently
        2, so a ``0`` would not in fact trigger anything today. It would the moment that
        threshold were lowered to 0 or 1, and then a read-back would fire for a binding
        that is not there. Saying so plainly beats implying a live trap, which is how a
        defensive choice loses the reason it was made.

        Performs NO save: every confirmed description push reaches this, and durability
        comes from the pass's later unconditional :meth:`save`, so persisting per emit
        would only add an fsync per push.
        """
        return self._lifecycle.note_rich_emit(local_id, wire)

    # -- comment-ID map (thin delegates; emersed-specific-mutt) -------------
    #
    # The append-only map, its write-ahead save and the DIG-5301 duplicate class it
    # closes are documented on ``BindingLifecycle.record_comment_id``.

    def record_comment_id(self, local_comment_key: str, jira_comment_id: str) -> None:
        """Persist a COMMENT-HLC-key -> comment-ID pairing and save NOW.

        The value is the transport's comment ID, or
        ``binding_lifecycle.UNKNOWN_COMMENT_ID`` when it cannot report one (bug aa7b).
        """
        self._lifecycle.record_comment_id(local_comment_key, jira_comment_id)

    def comment_id_for(self, local_comment_key: str) -> str | None:
        """The recorded value for an HLC key, or ``None`` when unmapped.

        May be ``binding_lifecycle.UNKNOWN_COMMENT_ID`` rather than a Jira comment ID;
        see ``BindingLifecycle.comment_id_for``.
        """
        return self._lifecycle.comment_id_for(local_comment_key)

    def is_comment_mapped(self, local_comment_key: str) -> bool:
        """True once :meth:`record_comment_id` has persisted this HLC key."""
        return self._lifecycle.is_comment_mapped(local_comment_key)

    def note_comment_posted(self) -> None:
        """Count a successful outbound post before comment-ID recording guards run. The
        end-of-pass invariant compares posts with IDs gained even when recording skips."""
        self._comment_posts += 1

    def comment_posts_this_pass(self) -> int:
        """Count successful outbound posts this pass."""
        return self._comment_posts

    def comment_ids_gained_this_pass(self) -> int:
        """Count comment IDs added since load."""
        return len(self._data.get("comment_ids", {})) - self._comment_ids_at_load

    # Absence lifecycle.

    def _entry_for_jira_key(self, jira_key: str) -> dict[str, Any] | None:
        """Find a binding entry through the reverse Jira-key index."""
        return self._lifecycle.entry_for_jira_key(jira_key)

    def note_absent(self, jira_key: str) -> None:
        """Record a consecutive 404. BindingLifecycle retires the binding at the configured
        grace threshold and alerts after its retired-first write."""
        self._lifecycle.note_absent(jira_key)

    def note_absent_or_rekey(self, jira_key: str, client: TicketTransport | None = None) -> bool:
        """On a confirmed 404, check the immutable Jira ID before recording absence.
        A project move can change the key without changing that ID. When the lookup finds
        a different key, BindingLifecycle updates the binding and reverse index together,
        resets absence, and returns True. Without a client, captured ID, resolvable key,
        or changed key, record absence and return False."""
        local_id = self._data["reverse"].get(jira_key)
        entry = self._entry_for_jira_key(jira_key)
        if entry is None or local_id is None:
            return False
        jira_id = entry.get("jira_id")
        if client is None or not jira_id:
            self.note_absent(jira_key)
            return False
        if self._lifecycle.rekey(jira_key, _current_key_by_id(client, str(jira_id))):
            return True
        self.note_absent(jira_key)
        return False

    def clear_absent(self, jira_key: str) -> None:
        """Reset the absence counter after a 200 GET (the issue is alive).

        Change-gated: an entry with no counter set is left completely untouched, so a
        healthy pass causes no store churn. See ``BindingLifecycle.clear_absent``.
        """
        self._lifecycle.clear_absent(jira_key)

    def set_last_get(self, jira_key: str, pass_id: str) -> None:
        """Record the pass_id of the most recent GET (rotation bookkeeping)."""
        entry = self._entry_for_jira_key(jira_key)
        if entry is None:
            return
        get_rotation.set_last_get(self._get_rotation, jira_key, pass_id)
        entry["last_get_pass"] = self._get_rotation[jira_key]

    def last_get_pass(self, jira_key: str) -> str:
        """Return the pass_id of the last GET; ``""`` if never GET'd (sorts first)."""
        entry = self._entry_for_jira_key(jira_key)
        legacy = entry.get("last_get_pass") if entry is not None else ""
        return get_rotation.latest(self._get_rotation, jira_key, legacy)

    def is_retired(self, jira_key: str) -> bool:
        """Return True if the key has been soft-deleted (retired)."""
        return self._lifecycle.is_retired(jira_key)

    # -- tombstones: the local-side view of retirement (thin delegates; 3b5f) -
    #
    # Why the local-side index exists at all, and why lifting a tombstone must clear
    # BOTH sides, is documented on the ``BindingLifecycle`` counterparts.

    def retired_key_for_local(self, local_id: str) -> str | None:
        """Return the retired Jira key, if any. This tombstone distinguishes a confirmed
        deleted pairing from a ticket that was never bound."""
        return self._lifecycle.retired_key_for_local(local_id)

    def is_retired_local(self, local_id: str) -> bool:
        """True when this local ticket's former Jira pairing was retired (3b5f)."""
        return self._lifecycle.is_retired_local(local_id)

    def unretire(self, jira_key: str) -> bool:
        """Lift a tombstone: drop ``jira_key`` from the retired set AND file (3b5f).

        The documented route back, so a suppressed create is not a permanent dead end.
        Returns True when a tombstone was actually lifted; False (a no-op) when the key
        was not retired, so the call is idempotent.
        """
        return self._lifecycle.unretire(jira_key)

    def note_create_suppressed(self, local_id: str, jira_key: str) -> None:
        """Record that a tombstone suppressed an outbound CREATE (3b5f).

        A suppression is work NOT done, so it must be loud: a silently-skipped create
        looks identical to a healthy steady state.
        """
        self._lifecycle.note_create_suppressed(local_id, jira_key)

    # -- recovery ----------------------------------------------------------

    def complete_interrupted_retirements(self) -> binding_recovery.RetirementRepairOutcome:
        """Finish retirements interrupted between the tombstone write and the live drop.

        The facade door onto ``BindingRecovery.complete_interrupted_retirements``, which
        owns the policy: an EXACT tombstone/forward/reverse match has its live residue
        removed under a single ``save()``, anything that disagrees is refused with
        evidence, and the tombstone itself is never touched — ``unretire`` remains the only
        route back from a soft delete.

        This is the UNGUARDED entry point and no pass calls it directly. A pass goes
        through :meth:`repair_at_write_boundary`, which decides whether the pass is even
        allowed to repair; this method assumes that decision has already been made. It
        stays public because the repair semantics need a direct oracle that does not have
        to satisfy the guard to be tested.

        Returns the outcome unchanged, both halves included — a repair that reported only
        its successes would let a permanently-refused tombstone look like a healthy store.
        """
        return self._recovery.complete_interrupted_retirements()

    def repair_at_write_boundary(
        self, *, persist: bool, scoped: bool
    ) -> binding_recovery.RetirementRepairOutcome:
        """The pass-level door onto interrupted-retirement repair, guard included.

        The facade door the reconcile spine calls once per pass, at the point where the
        pass has already taken the exclusive lock and cleared the under-lock staleness gate
        but has NOT yet made its first remote observation. Ordering is the whole reason the
        call site is where it is: a tombstone records authoritative retirement intent, and a
        pass that fetched first could see the retired issue answer 200 and then finish the
        retirement in the same breath as fresh evidence that the issue is alive. Repairing
        before the fetch keeps the recorded decision from racing a later observation.

        The ``persist``/``scoped`` policy is ``BindingRecovery.repair_at_write_boundary``'s
        and is deliberately NOT re-stated at the call site: a read-only pass and a filtered
        pass both get an empty outcome and no write. This is a thin delegate for the same
        reason the other recovery delegate is one — the recovery owner stays private, so no
        caller can reach the unguarded ``complete_interrupted_retirements`` on it without
        passing through this facade.
        """
        return self._recovery.repair_at_write_boundary(persist=persist, scoped=scoped)

    def recover_pending_bindings(
        self, client: TicketTransport, *, failure_sink: list[dict[str, Any]] | None = None
    ) -> int:
        """Recover pending creates through BindingRecovery. A keyed entry reattaches its
        label and property without search. A keyless entry searches current and legacy
        rebar-id labels, then confirms or corroborates absence before unbinding. Append
        per-entry errors to failure_sink and leave those entries pending. Return the
        number resolved."""
        return self._recovery.recover_pending_bindings(client, failure_sink=failure_sink)


def load_binding_store(repo_root: Path) -> BindingStore:
    """Entry point for the reconciler orchestrator — call at pass start.

    The store is RELOCATABLE, so the binding state directory is RESOLVED through
    ``rebar.config.tracker_dir`` (``REBAR_TRACKER_DIR`` > ``tracker.dir`` > the default
    ``.tickets-tracker`` under the repo root) rather than composed from the default
    name — otherwise a relocated deployment would read and write bindings in a
    directory the operator never configured.
    """
    from rebar.config import tracker_dir as _resolve_tracker_dir

    return BindingStore(_resolve_tracker_dir(repo_root))
