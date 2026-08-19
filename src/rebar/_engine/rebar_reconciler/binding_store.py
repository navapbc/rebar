"""Local binding store for Jira bidirectional sync.

Maps local ticket IDs ↔ Jira issue keys.  Persisted as JSON at
`.tickets-tracker/.bridge_state/bindings.json` on the tickets branch.  # tickets-boundary-ok

Neither persistence nor identity is implemented here.  ``BindingRepository`` (RP-02 S1)
owns the four state files — live store, retired sidecar, GET-rotation sidecar, alert log —
with their load rules, exact committed bytes, and asymmetric failure dispositions (live
fails CLOSED, retired and rotation fail OPEN); ``BindingLifecycle`` (RP-02 S2) owns the
identity transitions (bind/confirm/unbind, the immutable numeric id, the MOVED-issue
re-key).  This module is the FACADE over both, owning absence, retirement, tombstone and
comment bookkeeping.  It mutates the repository's OWN dictionaries in place — handed out
by reference, never copied — and then calls ``save()``, the single persistence boundary.

Write-ahead protocol (story 9622): bind_pending + save(); create_issue() →
DIG-NNNN; record_pending_key + save() — persisted on the STILL-pending entry
BEFORE the rebar-id label is attached; plant the label/property; bind_confirm +
save(). The keyed-pending write makes recovery deterministic: a hard-kill between
create and label leaves the ``jira_key`` on the pending entry, so recovery
re-attaches the label and confirms with NO Jira search (no duplicate).
``jira_key`` on a ``pending`` entry is an additive SUB-state of the ADR-0027
``pending`` state, not a new enumerated state.

Recovery (next pass, recover_pending_bindings): keyed-pending → retro-attach
label/property (idempotent) and confirm, NO search; keyless-pending → search Jira
for the rebar-id label, confirm if found else unbind; per-entry error → append
``{local_id, reason}`` to ``failure_sink`` and continue.

Comment sync (emersed-specific-mutt): a ``comment_ids`` map (local_comment_key
HLC → Jira comment ID) makes comment mirroring append-only/idempotent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._backend import TicketTransport

from rebar_reconciler import binding_lifecycle, get_rotation, peer_state
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


#: How long a keyless-pending binding is treated as "the create may have landed but is not
#: indexed yet" (bug 21fc). Jira DC's Lucene index is eventually consistent and
#: JRASERVER-70423 documents a 2,991s lag, so this is deliberately LARGER: the cost of
#: waiting is a delayed create, of not waiting a duplicate — only one is reversible.
_INDEX_LAG_GRACE_SECONDS = 3600.0

#: Consecutive negative searches required before absence is treated as corroborated (a
#: single miss is exactly what a lagging index produces for an issue that DOES exist).
_MISSES_BEFORE_UNBIND = 3


def _age_seconds(created_at: Any) -> float:
    """Seconds since an ISO-8601 ``created_at``; ``inf`` when it is absent or unparseable.

    ``inf`` is the SAFE default here and the direction matters: an entry whose age cannot
    be established is treated as OLD, so it becomes eligible for the ordinary
    corroborated-unbind path rather than being suppressed forever. A store written before
    this field existed must not strand its tickets.
    """
    if not isinstance(created_at, str) or not created_at:
        return float("inf")
    from datetime import datetime, timezone

    try:
        stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def _now_iso() -> str:
    # Canonical Z-suffix UTC (twin of rebar.timeutils.utc_now_iso); retained as the
    # local spelling used across this module's call sites.
    return utc_now_iso()


# Bug 1e08-1a35-0267-4ca6 — binding lifecycle (GC) defaults. These are the
# reconciler's only int-valued binding env vars; parsed defensively below so a
# typo'd ops value degrades to the default rather than aborting the pass.
_DEFAULT_ABSENT_RETIRE_GRACE = 3


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """Parse an int env var defensively: malformed → default; clamp >= minimum.

    The reconciler has no dotted-config reader, so lifecycle knobs are env
    vars (matches fetcher.py / applier.py). A typo'd value (e.g. ``"abc"``)
    must NOT abort the pass — fall back to the documented default.
    """
    raw = os.environ.get(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except (ValueError, TypeError):
            value = default
    if minimum is not None and value < minimum:
        value = minimum
    return value


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

    The FACADE, and the only public door to binding state. Two private owners sit behind
    it: :class:`BindingRepository` owns every byte on disk, and ``BindingLifecycle`` owns
    the identity transitions (bind/confirm/unbind, the immutable numeric id, the
    MOVED-issue re-key). This class owns what is left — retire/tombstone/comment/rotation
    policy, absence bookkeeping, and pending-binding recovery — and coordinates the two.
    Mutations are in-memory until ``save()``, which delegates the atomic write.
    """

    def __init__(self, tracker_dir: Path) -> None:
        """Open the binding state under ``tracker_dir``. READS ONLY — never writes.

        Every attribute below is an ALIAS for the repository's own object, never a copy:
        the identity transitions, the ``peer_state`` delegates and the rich-text handler
        all mutate ``bindings`` / ``reverse`` / the rotation stamps / the retired set IN
        PLACE, so a defensive copy here would silently discard those writes. The
        ``BindingLifecycle`` owner is constructed over the SAME repository for exactly
        that reason, and is deliberately private — no public attribute or method hands
        out either owner, or a caller could write binding state without passing through
        this facade's coordination. The repository does not materialize ``.bridge_state``
        either; the first :meth:`save` creates it.

        Failure dispositions come from the repository unchanged: a corrupt live store
        raises ``ValueError`` (fail CLOSED, to avoid mass-duplicating Jira issues); a
        corrupt retired file degrades to an empty set plus a deduped alert (fail OPEN).
        """
        self._repo = BindingRepository(tracker_dir)
        self._lifecycle = binding_lifecycle.BindingLifecycle(self._repo)
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
        self._retired: set[str] = self._repo.retired_keys()
        # Bug 3b5f: reverse of the retired file — {local_id: jira_key} — so the
        # outbound differ can ask "was THIS local ticket confirmed-deleted?" without
        # re-reading the file per unbound ticket. Kept in lock-step with ``_retired``
        # by ``_retire``/``unretire``; a legacy list-form file degrades to empty.
        self._retired_locals: dict[str, str] = self._repo.retired_locals()

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

    # -- comment-ID map (append-only comment sync; emersed-specific-mutt) ---

    def record_comment_id(self, local_comment_key: str, jira_comment_id: str) -> None:
        """Persist a COMMENT-HLC-key -> Jira-comment-ID pairing and ``save()`` NOW.

        The map identifies an already-mirrored comment by the COMMENT event's HLC
        ``timestamp`` (a stable, unique ``local_comment_key``), not by body — so
        same-text comments never collide and an edited body is not seen as new.
        Unlike :meth:`record_jira_id`, this ``save()``s IMMEDIATELY (write-ahead):
        it is called on the successful ``add_comment`` return, and the durable
        entry is what the outbound differ's PRIMARY skip keys on, so a crash after
        the Jira post cannot re-post (closes the DIG-5301 duplicate class).

        Append-only and idempotent: an identical re-record is a no-op (no ``save``
        churn); a key is never remapped to a different id.
        """
        key = str(local_comment_key)
        comment_ids = self._data.setdefault("comment_ids", {})
        if comment_ids.get(key) == str(jira_comment_id):
            return
        comment_ids[key] = str(jira_comment_id)
        self.save()

    def comment_id_for(self, local_comment_key: str) -> str | None:
        """The recorded Jira comment ID for an HLC key, or ``None`` when unmapped."""
        return self._data.get("comment_ids", {}).get(str(local_comment_key))

    def is_comment_mapped(self, local_comment_key: str) -> bool:
        """True once :meth:`record_comment_id` has persisted this HLC key."""
        return str(local_comment_key) in self._data.get("comment_ids", {})

    # -- absence lifecycle (bug 1e08) --------------------------------------

    def _entry_for_jira_key(self, jira_key: str) -> dict[str, Any] | None:
        """Resolve a binding entry by Jira key via the reverse index (delegated)."""
        return self._lifecycle.entry_for_jira_key(jira_key)

    def note_absent(self, jira_key: str) -> None:
        """Record a consecutive-404 GET against a bound key.

        Increments ``absent_404_count`` on the binding entry. When the count
        reaches ``RECONCILER_ABSENT_RETIRE_GRACE`` consecutive 404s, the
        binding is soft-deleted: moved to bindings-retired.json (reversible)
        and a deduped ``binding-retired`` alert is appended.
        """
        local_id = self._data["reverse"].get(jira_key)
        entry = self._entry_for_jira_key(jira_key)
        if entry is None:
            return
        entry["absent_404_count"] = int(entry.get("absent_404_count", 0)) + 1
        entry["updated_at"] = _now_iso()
        grace = _env_int(
            "RECONCILER_ABSENT_RETIRE_GRACE",
            _DEFAULT_ABSENT_RETIRE_GRACE,
            minimum=1,
        )
        if entry["absent_404_count"] >= grace:
            self._retire(local_id, jira_key, entry)

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

    def _retire(self, local_id: str, jira_key: str, entry: dict[str, Any]) -> None:
        """Soft-delete a binding: move it to the retired file + alert.

        RETIRED FIRST, live second (both writes go through the repository): the entry
        must be durable in the retired file BEFORE the live binding is dropped, or a
        crash between the two would lose it from both and make a soft delete
        indistinguishable from a hard one.
        """
        retired_entries = self._repo.retired_entries()
        retired_entries[jira_key] = {
            "local_id": local_id,
            "retired_at": _now_iso(),
            "absent_404_count": int(entry.get("absent_404_count", 0)),
            "last_jira_key": jira_key,
        }
        self._repo.save_retired(retired_entries)
        self._retired.add(jira_key)
        self._retired_locals[local_id] = jira_key
        # Remove the live binding (reversible: the entry survives in the
        # retired file and the live binding can be re-created on recovery).
        self._data["bindings"].pop(local_id, None)
        self._data["reverse"].pop(jira_key, None)
        self.save()
        self._repo.alert(
            key=f"binding-retired:{jira_key}",
            record={
                "kind": "binding-retired",
                "jira_key": jira_key,
                "local_id": local_id,
            },
        )

    def clear_absent(self, jira_key: str) -> None:
        """Reset the absence counter after a 200 GET (the issue is alive)."""
        entry = self._entry_for_jira_key(jira_key)
        if entry is None:
            return
        if entry.get("absent_404_count"):
            entry["absent_404_count"] = 0
            entry["updated_at"] = _now_iso()

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
        return jira_key in self._retired

    # -- tombstones: the local-side view of retirement (bug 3b5f) -----------

    def retired_key_for_local(self, local_id: str) -> str | None:
        """The retired ``jira_key`` this local ticket was last bound to, or ``None``.

        The reverse of ``is_retired``, which is keyed by ``jira_key`` and therefore
        cannot answer the question the unbound-create arm needs to ask: retirement
        UNBINDS the local ticket (``_retire`` pops it from both ``bindings`` and
        ``reverse``), so by the time the differ sees it there is no live key to look
        up — only this tombstone distinguishes "was paired with a confirmed-deleted
        issue" from "never bound at all".
        """
        key = self._retired_locals.get(local_id)
        return key if key is not None and key in self._retired else None

    def is_retired_local(self, local_id: str) -> bool:
        """True when this local ticket's former Jira pairing was retired (3b5f)."""
        return self.retired_key_for_local(local_id) is not None

    def unretire(self, jira_key: str) -> bool:
        """Lift a tombstone: drop ``jira_key`` from the retired set AND file (3b5f).

        The documented route back from a tombstone. After this call the local ticket
        is ordinary unbound work again, so the next pass's outbound differ creates a
        fresh Jira issue for it — without this, suppressing the create would be a
        permanent, undocumented dead end escapable only by hand-editing
        ``.bridge_state/bindings-retired.json``.

        Returns True when a tombstone was actually lifted; False (a no-op) when the
        key was not retired, so the call is idempotent.
        """
        retired_entries = self._repo.retired_entries()
        entry = retired_entries.pop(jira_key, None)
        if entry is None and jira_key not in self._retired:
            return False
        self._repo.save_retired(retired_entries)
        self._retired.discard(jira_key)
        for local_id, key in list(self._retired_locals.items()):
            if key == jira_key:
                del self._retired_locals[local_id]
        self._repo.alert(
            key=f"binding-unretired:{jira_key}",
            record={
                "kind": "binding-unretired",
                "jira_key": jira_key,
                "local_id": (entry or {}).get("local_id", ""),
            },
        )
        return True

    def note_create_suppressed(self, local_id: str, jira_key: str) -> None:
        """Record that a tombstone suppressed an outbound CREATE (3b5f).

        A suppression is work NOT done, so it must be loud: a silently-skipped
        create looks identical to a healthy steady state. The alert names the route
        back so the operator does not have to hand-edit retired state.
        """
        self._repo.alert(
            key=f"outbound-create-suppressed:{local_id}",
            record={
                "kind": "outbound-create-suppressed",
                "local_id": local_id,
                "jira_key": jira_key,
                "reason": (
                    f"Jira issue {jira_key} was confirmed deleted (404 to grace) and the "
                    f"binding retired; a deleted issue is never re-created."
                ),
                "remedy": f"BindingStore.unretire({jira_key!r}) to re-enable creation",
            },
        )

    # -- recovery ----------------------------------------------------------

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
        """
        recovered = 0
        for local_id in list(self.pending_bindings()):
            try:
                entry = self._data["bindings"].get(local_id) or {}
                keyed = entry.get("jira_key")
                if keyed:
                    # Deterministic: the key is known — retro-attach the identity
                    # marker (idempotent) so future JQL dedup can find the issue,
                    # then confirm. No search.
                    client.add_label(keyed, f"rebar-id:{local_id}")
                    client.set_entity_property(keyed, "local_id", local_id)
                    self.bind_confirm(local_id, keyed)
                    recovered += 1
                    continue
                # Keyless: canonical colon-form label (applier.py outbound/inbound).
                colon_label = f"rebar-id:{local_id}"
                results = client.search_issues(f'labels = "{colon_label}"')
                if not results:
                    # Legacy fallback: hyphen-form (pre-colon-migration issues).
                    hyphen_label = f"rebar-id-{local_id}"
                    results = client.search_issues(f'labels = "{hyphen_label}"')
                if results:
                    self.bind_confirm(local_id, results[0]["key"])
                    recovered += 1
                    continue
                # A NEGATIVE SEARCH IS NOT PROOF OF ABSENCE ON DC (bug 21fc): the keyless
                # state is entered on a crash during create_issue — exactly when the issue
                # may exist but not yet be indexed (Jira DC's Lucene index is eventually
                # consistent, JRASERVER-70423: 2,991s lag). Unbinding here makes the next
                # pass create a duplicate, so absence must be CORROBORATED: repeated misses
                # AND an entry too old for the documented lag to explain. Else stay pending.
                misses = int(entry.get("search_miss_count") or 0) + 1
                entry["search_miss_count"] = misses
                entry["updated_at"] = _now_iso()
                if (
                    misses >= _MISSES_BEFORE_UNBIND
                    and _age_seconds(entry.get("created_at")) >= _INDEX_LAG_GRACE_SECONDS
                ):
                    self.unbind(local_id)
                    recovered += 1
            except Exception as exc:  # noqa: BLE001 — loud-but-non-fatal: record and continue
                if failure_sink is not None:
                    failure_sink.append({"local_id": local_id, "reason": repr(exc)})
        return recovered


def load_binding_store(repo_root: Path) -> BindingStore:
    """Entry point for the reconciler orchestrator — call at pass start."""
    tracker_dir = repo_root / ".tickets-tracker"  # tickets-boundary-ok
    return BindingStore(tracker_dir)
