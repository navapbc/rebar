"""Local binding store for Jira bidirectional sync.

Maps local ticket IDs ↔ Jira issue keys.  Persisted as JSON at
`.tickets-tracker/.bridge_state/bindings.json` on the tickets branch.  # tickets-boundary-ok

Neither persistence nor lifecycle policy is implemented here.  ``BindingRepository``
(RP-02 S1) owns the four state files — live store, retired sidecar, GET-rotation sidecar,
alert log — with their load rules, exact committed bytes, and asymmetric failure
dispositions (live fails CLOSED, retired and rotation fail OPEN); ``BindingLifecycle``
(RP-02 S2) owns the identity transitions (bind/confirm/unbind, the immutable numeric id,
the MOVED-issue re-key) and, since S2 T2, absence, retirement, tombstone and comment
bookkeeping; ``BindingRecovery`` (RP-02 S3) owns incomplete-operation repair — pending-binding
recovery and interrupted-retirement completion.  This module is the FACADE over the three,
owning GET-rotation and coordinating them.  Everything it does mutates the repository's OWN
dictionaries in place — handed out by reference, never copied — and then calls ``save()``,
the single persistence boundary.

Write-ahead protocol (story 9622): bind_pending + save(); create_issue() →
DIG-NNNN; record_pending_key + save() — persisted on the STILL-pending entry
BEFORE the rebar-id label is attached; plant the label/property; bind_confirm +
save(). The keyed-pending write makes recovery deterministic: a hard-kill between
create and label leaves the ``jira_key`` on the pending entry, so recovery
re-attaches the label and confirms with NO Jira search (no duplicate).
``jira_key`` on a ``pending`` entry is an additive SUB-state of the ADR-0027
``pending`` state, not a new enumerated state.

Recovery (next pass, recover_pending_bindings — the policy is ``binding_recovery``'s
since RP-02 S3 T1; this facade only delegates): keyed-pending → retro-attach
label/property (idempotent) and confirm, NO search; keyless-pending → search Jira
for the rebar-id label, confirm if found else unbind; per-entry error → append
``{local_id, reason}`` to ``failure_sink`` and continue.

Comment sync (emersed-specific-mutt): a ``comment_ids`` map (local_comment_key
HLC → Jira comment ID) makes comment mirroring append-only/idempotent; the map's
write-ahead and change-gating rules live with the policy in ``binding_lifecycle``.
"""

from __future__ import annotations

# ``os`` is retained for its MODULE-LEVEL HANDLE, not for a call in this file. The env
# read that used to consume it moved to ``binding_lifecycle`` with the absence policy it
# parameterizes (RP-02 S2 T2), but ``binding_store.os`` is a long-standing part of this
# module's surface: the atomic-write crash oracles reach ``os.replace`` through it to fail
# the live replacement mid-save. Dropping the import would break them for no gain.
import os  # noqa: F401
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._backend import TicketTransport

from rebar_reconciler import binding_lifecycle, binding_recovery, get_rotation, peer_state
from rebar_reconciler.binding_repository import BindingRepository
from rebar_reconciler.timeutil import utc_now_iso


class BindingPersistError(RuntimeError):
    """A write-ahead binding persist (``save()``) failed before ``create_issue``.

    Raised by the outbound-create write-ahead path (dispatch_one) when the
    durable pending record cannot be persisted. A durable pending record is a
    PRECONDITION for a safe create (it is what recovery keys on), so the create
    is skipped rather than run without one — the mutation is recorded failed and
    an alert fires, and the pass continues with the remaining mutations.
    """


# The index-lag grace, the corroborated-miss threshold and the age helper now live in
# ``binding_recovery`` with the pending-recovery policy they parameterize (RP-02 S3 T1).
# These are ALIASES, not a second source of truth: ``is_keyless_pending_within_grace``
# below still reads the grace and the age, and the constants are part of this module's
# long-standing inspection surface (the bug-21fc duplicate oracles reach the lag window
# through it). Rebinding either constant here would desynchronize it from the recovery
# that actually enforces it, so treat these three names as read-only labels.
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


# Bug 1e08-1a35-0267-4ca6 — the absence retirement grace default. The env read itself is
# owned by ``rebar.config.resolve_absent_retire_grace`` (RP-04 S7.3.a); this constant is
# the shared fallback default, defined in ``binding_lifecycle``. This is an ALIAS, not a
# second source of truth, kept because the constant is part of this module's long-standing
# inspection surface (the 3b5f tombstone oracle drives retirement to grace through it).
_DEFAULT_ABSENT_RETIRE_GRACE = binding_lifecycle._DEFAULT_ABSENT_RETIRE_GRACE


def _current_key_by_id(client: TicketTransport, jira_id: str) -> str:
    """The issue's CURRENT key, looked up by its immutable numeric id (bug 7c26).

    ``GET /rest/api/{2,3}/issue/{issueIdOrKey}`` accepts an id wherever it accepts a
    key on BOTH deployments, so this reuses ``get_issue_by_rest`` (the same
    primary-store read the absence probe uses — not subject to search-index lag,
    bug 21fc). Returns ``""`` — "no answer, treat the absence as real" — when the
    client predates the member, the lookup raises (incl. a genuine-gone 404), or the
    payload carries no string key, so every failure mode falls THROUGH to the
    unchanged absence bookkeeping rather than masking a deletion.
    """
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
        """Open the binding state under ``tracker_dir``. READS ONLY — never writes.

        Every attribute below is an ALIAS for the repository's own object, never a copy:
        the lifecycle transitions, the ``peer_state`` delegates and the rich-text handler
        all mutate ``bindings`` / ``reverse`` / the rotation stamps IN PLACE, so a
        defensive copy here would silently discard those writes. The ``BindingLifecycle``
        and ``BindingRecovery`` owners are constructed over the SAME repository for exactly
        that reason (recovery also takes the lifecycle owner, so a repaired binding goes
        through the same transitions an ordinary one does), and all three are deliberately
        private — no public attribute or method hands out an owner, or a caller could write
        binding state without passing through this facade's
        coordination. The retired key set and the bug-3b5f ``{local_id: jira_key}``
        tombstone index are reached through the lifecycle owner rather than aliased here,
        so retired state has exactly one reader. The repository does not materialize
        ``.bridge_state`` either; the first :meth:`save` creates it.

        Failure dispositions come from the repository unchanged: a corrupt live store
        raises ``ValueError`` (fail CLOSED, to avoid mass-duplicating Jira issues); a
        corrupt retired file degrades to an empty set plus a deduped alert (fail OPEN).
        """
        self._repo = BindingRepository(tracker_dir)
        self._lifecycle = binding_lifecycle.BindingLifecycle(self._repo)
        self._recovery = binding_recovery.BindingRecovery(self._repo, self._lifecycle)
        # The four locations the repository writes, mirrored here so the facade's
        # long-standing inspection surface is unchanged. They are READ-ONLY labels now:
        # the live store, the bug-1e08 retired sidecar (soft deletes live beside the live
        # store so retirement stays reversible), the GET-rotation sidecar, and the repo
        # root the lifecycle alerts are keyed off (``<repo_root>/.tickets-tracker`` is
        # ``tracker_dir``). Nothing here opens them — the repository does.
        self._path = self._repo.path
        self._retired_path = self._repo.retired_path
        self._get_rotation_path = self._repo.rotation_path
        self._repo_root = self._repo.repo_root
        self._data = self._repo.data
        self._get_rotation = self._repo.rotation

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
        """True while a KEYLESS-pending binding is young enough that a negative Jira
        search proves nothing — i.e. the create may have landed but not indexed.

        This is the half that actually prevents a duplicate. ``outbound_differ`` gates
        the create on ``get_jira_key(local_id) is None``, and a keyless-pending entry
        HAS ``jira_key: None`` — so that branch cannot tell "never created" from
        "created, then we crashed before recording the key, and Jira has not indexed it
        yet". Without this signal the create is emitted while recovery is still waiting
        out the index lag, and ``create_one``'s dedup search misses for the SAME
        eventual-consistency reason, writing a SECOND Jira issue (bug 21fc, the only
        known path where rebar writes wrong data rather than reading incomplete data).

        Deferring is safe both ways: if the issue exists,
        ``recover_pending_bindings`` binds it once the index catches up; if it never
        landed, the grace expires and the create is emitted later. KEYLESS only: a
        keyed-pending entry is recovered deterministically by retro-attach.
        """
        entry = self._data["bindings"].get(local_id)
        if entry is None or entry.get("state") != "pending" or entry.get("jira_key"):
            return False
        return _age_seconds(entry.get("created_at")) < _INDEX_LAG_GRACE_SECONDS

    def all_bindings(self) -> dict[str, dict]:
        """A SHALLOW copy of the ``{local_id: entry}`` map.

        Fresh outer mapping (so a caller may iterate it while the lifecycle adds or
        removes bindings), but the inner entry dicts are the LIVE ones. Callers rely on
        that: the baseline advance and the rich-text handler mutate an entry they got
        from here and expect the next ``save()`` to persist it. Deep-copying would
        silently drop those writes.
        """
        return dict(self._data["bindings"])

    def pending_bindings(self) -> list[str]:
        return [
            lid for lid, entry in self._data["bindings"].items() if entry.get("state") == "pending"
        ]

    def confirmed_count(self) -> int:
        return sum(
            1 for entry in self._data["bindings"].values() if entry.get("state") == "confirmed"
        )

    # -- identity transitions (thin delegates to BindingLifecycle) ----------
    #
    # The write-ahead progression described in this module's docstring is implemented in
    # ``binding_lifecycle.py``, over the SAME dictionaries this facade aliases; read the
    # rationale for each transition there (the c244 rebind reverse cleanup, the 874a
    # unbind sweep, the bug-7c26 numeric id). These wrappers exist so the mature caller
    # contract — the reconciler, the adapters and ``bridge fsck`` all bind to
    # ``BindingStore`` — does not move.

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
        """The issue's IMMUTABLE numeric Jira id for a binding, or None.

        ``None`` is VALID and means "not captured yet" — every binding written before bug
        7c26 has no id, so :meth:`note_absent_or_rekey` degrades to its pre-7c26
        behaviour for it and no migration is required.
        """
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
        ``None`` when the local id is unbound — distinct from ``0``, which would read as
        a first push and trigger the caller's read-back for a binding that is not there.
        A missing binding is nonfatal.

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
        """Persist a COMMENT-HLC-key -> Jira-comment-ID pairing and save NOW."""
        self._lifecycle.record_comment_id(local_comment_key, jira_comment_id)

    def comment_id_for(self, local_comment_key: str) -> str | None:
        """The recorded Jira comment ID for an HLC key, or ``None`` when unmapped."""
        return self._lifecycle.comment_id_for(local_comment_key)

    def is_comment_mapped(self, local_comment_key: str) -> bool:
        """True once :meth:`record_comment_id` has persisted this HLC key."""
        return self._lifecycle.is_comment_mapped(local_comment_key)

    # -- absence lifecycle (bug 1e08) --------------------------------------

    def _entry_for_jira_key(self, jira_key: str) -> dict[str, Any] | None:
        """Resolve a binding entry by Jira key via the reverse index (delegated)."""
        return self._lifecycle.entry_for_jira_key(jira_key)

    def note_absent(self, jira_key: str) -> None:
        """Record a consecutive-404 GET against a bound key.

        Increments ``absent_404_count``; at ``RECONCILER_ABSENT_RETIRE_GRACE``
        consecutive 404s the binding is soft-deleted (reversibly) and alerted. The
        threshold, its defensive ambient parse, and the retired-first write order are
        ``BindingLifecycle``'s.
        """
        self._lifecycle.note_absent(jira_key)

    def note_absent_or_rekey(self, jira_key: str, client: TicketTransport | None = None) -> bool:
        """404 bookkeeping that first asks whether the issue MOVED (bug 7c26).

        A bound key stops resolving for TWO different reasons, and the pre-7c26 code
        could not tell them apart: the issue was deleted, or the issue was MOVED to
        another project and re-keyed. Old keys are normally stacked in Jira's
        ``moved_issue_key`` table so the old key still resolves, but a
        Data-Center-specific Atlassian KB documents third-party apps moving issues via
        post-functions/automations failing to update that table — after which the old
        key stops resolving entirely. Treating that as a deletion silently detaches a
        live issue from its local ticket and, at grace, retires the binding.

        So before recording an absence we re-ask by the one identifier a move cannot
        change — the numeric id. On a hit whose key DIFFERS, the binding is re-keyed
        (and the reverse index updated in the SAME operation, or the old key would keep
        resolving to this local id and re-detach next pass), the absence counter is
        reset, and the issue is reported PRESENT.

        Returns True when the binding was re-keyed, False when the absence was recorded
        exactly as :meth:`note_absent` always did.

        DEGRADES TO TODAY'S BEHAVIOUR in every other case — no client, no captured id
        (every pre-7c26 binding), an unresolvable id, or a key that is unchanged. That
        is what makes this safe on the shared Cloud path and on an unmigrated store:
        the recovery can only ADD a save, never skip an absence it did not disprove.

        Only ever reached from a CONFIRMED 404, so the happy path never pays for it.

        This is the COORDINATOR of the seam and keeps the parts that are not identity
        policy: the guard, the by-id client lookup, and the absence fall-through. The
        re-key mutation itself — including the "did the key actually change?" comparison
        that decides it — belongs to ``BindingLifecycle.rekey``, which declines an
        unresolvable or unchanged answer, so every one of those cases still falls through
        to :meth:`note_absent` exactly as before.
        """
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
        """The retired ``jira_key`` this local ticket was last bound to, or ``None``.

        Retirement UNBINDS the local ticket, so there is no live key to look up by the
        time the outbound differ sees it; only this tombstone distinguishes "was paired
        with a confirmed-deleted issue" from "never bound at all".
        """
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

        Not called from anywhere in the pass yet, deliberately: choosing WHERE a repair
        runs inside a reconcile pass is its own decision, and wiring it before the classifier
        had a direct oracle would have made the first failure a reconcile failure.

        Returns the outcome unchanged, both halves included — a repair that reported only
        its successes would let a permanently-refused tombstone look like a healthy store.
        """
        return self._recovery.complete_interrupted_retirements()

    def recover_pending_bindings(
        self, client: TicketTransport, *, failure_sink: list[dict[str, Any]] | None = None
    ) -> int:
        """Scan for pending bindings and attempt to recover (story 9622).

        For each pending binding:

        - **Keyed-pending** (entry carries a ``jira_key`` — the write-ahead
          recorded it the instant ``create_issue`` returned, before the label was
          attached): the create landed, so retro-attach the rebar-id label +
          ``local_id`` entity property (idempotent) and confirm. NO Jira search —
          deterministic, so a crash in the create->label window yields NO duplicate.
        - **Keyless-pending** (no ``jira_key`` — crash before/during create): search
          Jira for the ``rebar-id:{local_id}`` label (colon form, legacy hyphen
          fallback). Confirm if found; unbind if not (the create never reached Jira).

        Any per-entry error is appended to ``failure_sink`` as ``{local_id, reason}``
        and skipped (loud but non-fatal; the entry stays pending). Returns the count
        of RESOLVED bindings (confirmed or unbound). ``client`` must expose
        ``search_issues`` / ``add_label`` / ``set_entity_property``.

        A thin delegate to ``BindingRecovery`` since RP-02 S3 T1. The name, the
        ``failure_sink`` keyword and the resolved-count return are UNCHANGED, because the
        mature caller contract binds to this facade — ``run_differs`` calls exactly this
        signature, and the move must be invisible to it.
        """
        return self._recovery.recover_pending_bindings(client, failure_sink=failure_sink)


def load_binding_store(repo_root: Path) -> BindingStore:
    """Entry point for the reconciler orchestrator — call at pass start."""
    tracker_dir = repo_root / ".tickets-tracker"  # tickets-boundary-ok
    return BindingStore(tracker_dir)
