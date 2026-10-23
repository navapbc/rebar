"""Own binding identity and absence transitions over repository state.

Lifecycle handles pending/confirmed bindings, numeric Jira IDs, rekeys,
404 grace, retirement, tombstones, and comment IDs. It mutates the repository's
live dictionaries in place so unknown fields survive and saves persist the
changes. ``BindingStore`` remains the public facade; recovery and GET rotation
live in their respective owners.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rebar.reconciler import peer_state
from rebar.reconciler.timeutil import utc_now_iso

if TYPE_CHECKING:
    from rebar.reconciler.binding_repository import BindingRepository

__all__ = ["UNKNOWN_COMMENT_ID", "BindingLifecycle"]

# ACLI may report no comment ID. This sentinel records that the local comment
# was mirrored without mistaking a work-item ID for a comment ID.
UNKNOWN_COMMENT_ID = "__rebar_unknown_comment_id__"


def _now_iso() -> str:
    # Use the shared canonical UTC timestamp spelling.
    return utc_now_iso()


# Shared default for consecutive-404 retirement grace; config resolves overrides.
_DEFAULT_ABSENT_RETIRE_GRACE = 3


class BindingLifecycle:
    """Owns forward/reverse identity transitions over the repository's own dictionaries."""

    def __init__(self, repository: BindingRepository) -> None:
        """Attach to ``repository``'s live state. Holds the repository, not its contents.

        Only the repository reference is retained. The two index dictionaries are read
        back off it at every access (see :attr:`_bindings` / :attr:`_reverse`) rather than
        snapshotted here, so this owner can neither drift from nor shadow the document the
        repository serializes.
        """
        self._repo = repository

    @property
    def _bindings(self) -> dict[str, Any]:
        """The repository's own ``{local_id: entry}`` map — the SAME object, never a copy."""
        return self._repo.bindings

    @property
    def _reverse(self) -> dict[str, Any]:
        """The repository's own ``{jira_key: local_id}`` index — the SAME object."""
        return self._repo.reverse

    @property
    def _data(self) -> dict[str, Any]:
        """The repository's own whole loaded document — the SAME object.

        Needed by the comment-identity map, which lives at the TOP level of the store
        rather than on a binding entry. A copy here would be the one delegation defect
        the rest of the suite cannot see: a shallow copy still shares ``bindings`` and
        ``reverse``, so only a NEW top-level key (exactly what
        :meth:`record_comment_id`'s ``setdefault`` inserts on a legacy store) would fail
        to reach the serialized bytes.
        """
        return self._repo.data

    @property
    def _retired(self) -> set[str]:
        """The repository's own retired-key set — the SAME object, kept in lock-step
        with the retired file by :meth:`retire` / :meth:`unretire`."""
        return self._repo.retired_keys()

    @property
    def _retired_locals(self) -> dict[str, str]:
        """The repository's own ``{local_id: jira_key}`` tombstone index — the SAME
        object (bug 3b5f). Retirement UNBINDS the local ticket, so this is the only thing
        that distinguishes "was paired with a confirmed-deleted issue" from "never
        bound"."""
        return self._repo.retired_locals()

    # -- pending / confirmed transitions -----------------------------------

    def bind_pending(self, local_id: str) -> None:
        """Mark a local ticket as pending outbound creation."""
        now = _now_iso()
        self._bindings[local_id] = {
            "jira_key": None,
            "state": "pending",
            "created_at": now,
            "updated_at": now,
        }

    def record_pending_key(self, local_id: str, jira_key: str) -> None:
        """Record the Jira key on a STILL-pending entry (write-ahead step 3).

        Called the instant ``create_issue`` returns a key, BEFORE the rebar-id
        label is attached, so a crash in the create->label window leaves a pending
        entry recovery can confirm deterministically (no search). The entry stays
        ``state='pending'``; if none exists yet (defensive), one is created.
        """
        now = _now_iso()
        entry = self._bindings.get(local_id)
        if entry is None:
            entry = {"state": "pending", "created_at": now}
            self._bindings[local_id] = entry
        entry["jira_key"] = jira_key
        entry["state"] = "pending"
        entry["updated_at"] = now

    def bind_confirm(self, local_id: str, jira_key: str) -> None:
        """Confirm binding after Jira issue creation succeeds."""
        now = _now_iso()
        entry = self._bindings.get(local_id)
        if entry is None:
            # Direct confirm without prior pending — allowed for recovery
            entry = {"created_at": now}
            self._bindings[local_id] = entry
        # Read the OLD key BEFORE overwriting so a rebind (e.g. hard-delete ->
        # re-create binds local_id to a NEW jira_key) drops the stale reverse entry
        # in the SAME save — otherwise reverse[old_key] dangles at this local_id
        # forever (c244; there is no dedicated rebind method, only unbind cleaned it).
        old_key = entry.get("jira_key")
        entry["jira_key"] = jira_key
        entry["state"] = "confirmed"
        entry["updated_at"] = now
        if old_key and old_key != jira_key:
            self._reverse.pop(old_key, None)
        # Maintain reverse index
        self._reverse[jira_key] = local_id

    def unbind(self, local_id: str) -> None:
        """Remove binding (for cleanup/rollback), clearing BOTH indexes.

        Gating the reverse pop on the forward entry's ``jira_key`` made cleanup
        of one index depend on the other, which this method has just destroyed:
        a keyless forward entry stranded its reverse key permanently, reported
        by ``bridge fsck`` as ``reverse_missing_forward`` forever (874a). The
        keyed pop stays an O(1) fast path; the sweep then clears any reverse key
        still pointing at ``local_id`` — including one orphaned out of band (a
        prune, a manual edit, a ``merge=ours`` artifact), which is what the
        ``bridge fsck --repair`` prune verb relies on.
        """
        entry = self._bindings.pop(local_id, None)
        reverse = self._reverse
        if entry is not None and entry.get("jira_key"):
            reverse.pop(entry["jira_key"], None)
        for jira_key in [key for key, value in reverse.items() if value == local_id]:
            reverse.pop(jira_key, None)

    # -- immutable numeric id (bug 7c26) -----------------------------------

    def get_jira_id(self, local_id: str) -> str | None:
        """Return the issue's immutable numeric ID, or None when not captured."""
        entry = self._bindings.get(local_id)
        if entry is None:
            return None
        jira_id = entry.get("jira_id")
        return str(jira_id) if jira_id else None

    def record_jira_id(self, local_id: str, jira_id: str) -> None:
        """Capture a numeric Jira ID on an existing binding until the next save.

        Missing bindings, empty IDs, and unchanged values are no-ops.
        """
        entry = self._bindings.get(local_id)
        if entry is None or not jira_id:
            return
        if entry.get("jira_id") == str(jira_id):
            return
        entry["jira_id"] = str(jira_id)
        entry["updated_at"] = _now_iso()

    def rekey(self, jira_key: str, current_key: str) -> bool:
        """Swap both indexes to a moved issue's new key and clear absence count.

        Empty or unchanged lookup results leave absence handling to the caller.
        A real rekey preserves other entry fields, saves immediately, and emits
        an alert. Return whether a binding changed.
        """
        local_id = self._reverse.get(jira_key)
        entry = self.entry_for_jira_key(jira_key)
        if entry is None or local_id is None:
            return False
        if not current_key or current_key == jira_key:
            return False
        entry["jira_key"] = current_key
        entry["absent_404_count"] = 0
        entry["updated_at"] = _now_iso()
        self._reverse.pop(jira_key, None)
        self._reverse[current_key] = local_id
        self._repo.save()
        self._repo.alert(
            key=f"binding-rekeyed:{jira_key}",
            record={
                "kind": "binding-rekeyed",
                "jira_key": current_key,
                "previous_jira_key": jira_key,
                "local_id": local_id,
            },
        )
        return True

    # -- queries used by the transitions -----------------------------------

    def entry_for_jira_key(self, jira_key: str) -> dict[str, Any] | None:
        """Resolve a binding entry by Jira key via the reverse index."""
        local_id = self._reverse.get(jira_key)
        if local_id is None:
            return None
        entry: dict[str, Any] | None = self._bindings.get(local_id)
        return entry

    # -- absence lifecycle (bug 1e08) --------------------------------------

    def note_absent(self, jira_key: str) -> None:
        """Record a consecutive-404 GET against a bound key.

        Increments ``absent_404_count`` on the binding entry. When the count
        reaches ``RECONCILER_ABSENT_RETIRE_GRACE`` consecutive 404s, the
        binding is soft-deleted: moved to bindings-retired.json (reversible)
        and a deduped ``binding-retired`` alert is appended.
        """
        local_id = self._reverse.get(jira_key)
        entry = self.entry_for_jira_key(jira_key)
        if entry is None or local_id is None:
            return
        entry["absent_404_count"] = int(entry.get("absent_404_count", 0)) + 1
        entry["updated_at"] = _now_iso()
        from rebar.config import resolve_absent_retire_grace

        grace = resolve_absent_retire_grace()
        if entry["absent_404_count"] >= grace:
            self.retire(local_id, jira_key, entry)

    def clear_absent(self, jira_key: str) -> None:
        """Reset the absence counter after a 200 GET (the issue is alive).

        CHANGE-GATED: an entry with no counter set is left untouched — not even
        ``updated_at`` is stamped. Every healthy pass calls this for every bound key, so
        an unconditional reset would rewrite (and re-commit) the whole store on a pass
        where nothing actually happened.
        """
        entry = self.entry_for_jira_key(jira_key)
        if entry is None:
            return
        if entry.get("absent_404_count"):
            entry["absent_404_count"] = 0
            entry["updated_at"] = _now_iso()

    # -- retirement: the soft delete, RETIRED FIRST -------------------------

    def retire(self, local_id: str, jira_key: str, entry: dict[str, Any]) -> None:
        """Soft-delete a binding: move it to the retired file + alert.

        RETIRED FIRST, live second (both writes go through the repository): the entry
        must be durable in the retired file BEFORE the live binding is dropped, or a
        crash between the two would lose it from both and make a soft delete
        indistinguishable from a hard one. That order is a contract, not an
        implementation detail — the crash window it leaves is ONE exact identity present
        both live and tombstoned, which is detectable and completable precisely because
        the identity matches on both sides (ADR 0099 §5).

        The retired file is rewritten WHOLESALE, so the merge always starts from a fresh
        :meth:`BindingRepository.retired_entries` read: working from a stale snapshot
        would drop other operators' tombstones and any unknown fields on them.
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
        self._bindings.pop(local_id, None)
        self._reverse.pop(jira_key, None)
        self._repo.save()
        self._repo.alert(
            key=f"binding-retired:{jira_key}",
            record={
                "kind": "binding-retired",
                "jira_key": jira_key,
                "local_id": local_id,
            },
        )

    def is_retired(self, jira_key: str) -> bool:
        """Return True if the key has been soft-deleted (retired)."""
        return jira_key in self._retired

    # -- tombstones: the local-side view of retirement (bug 3b5f) -----------

    def retired_key_for_local(self, local_id: str) -> str | None:
        """Return a local ticket's retired Jira key, if its tombstone remains."""
        key = self._retired_locals.get(local_id)
        return key if key is not None and key in self._retired else None

    def is_retired_local(self, local_id: str) -> bool:
        """Whether this local ticket has a retired Jira pairing."""
        return self.retired_key_for_local(local_id) is not None

    def unretire(self, jira_key: str) -> bool:
        """Remove a Jira tombstone and its local reverse entries.

        The local ticket becomes unbound and eligible for a new create.
        Return whether a tombstone was lifted.
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

    # -- rich-text emission state (morose-selfaware-unicorn) ---------------

    def note_rich_emit(self, local_id: str, wire: Any) -> int | None:
        """Record a confirmed description push on the live binding entry.

        Delegate repeat counting to ``peer_state``. Return the consecutive count
        (0 on first push) or None for an unbound ID. Mutate in memory; the pass's
        later save provides durability.
        """
        entry = self._bindings.get(local_id)
        if not isinstance(entry, dict):
            return None
        return peer_state.note_rich_emit(entry, wire)

    # -- comment IDs for mirrored comments ---

    def record_comment_id(self, local_comment_key: str, jira_comment_id: str) -> None:
        """Persist a local comment HLC key and Jira comment ID immediately.

        Use ``UNKNOWN_COMMENT_ID`` when transport reports no ID. Keys prevent
        duplicate posts even for identical bodies. Equal re-records do nothing;
        different IDs overwrite and save.
        """
        key = str(local_comment_key)
        comment_ids = self._data.setdefault("comment_ids", {})
        if comment_ids.get(key) == str(jira_comment_id):
            return
        comment_ids[key] = str(jira_comment_id)
        self._repo.save()

    def comment_id_for(self, local_comment_key: str) -> str | None:
        """Return the mapped ID or unknown-ID sentinel, or None if unmapped."""
        recorded: str | None = self._data.get("comment_ids", {}).get(str(local_comment_key))
        return recorded

    def is_comment_mapped(self, local_comment_key: str) -> bool:
        """True once :meth:`record_comment_id` has persisted this HLC key."""
        return str(local_comment_key) in self._data.get("comment_ids", {})
