"""Identity-transition policy owner for binding state (RP-02 S2).

``BindingLifecycle`` owns the IDENTITY half of binding lifecycle policy: the
``pending`` → keyed-``pending`` → ``confirmed`` progression, ``unbind``'s two-index
teardown, capture of the immutable numeric Jira id (bug 7c26), and the re-key that keeps a
binding attached to an issue that MOVED project.

It is not a store and not a persistence owner. :class:`BindingRepository` owns every byte
on disk; this class mutates the repository's OWN ``bindings`` / ``reverse`` dictionaries
IN PLACE. They are reached back through the repository on every access — never copied,
never cached — because a copy at this boundary would look correct in memory and silently
discard every transition at save time. Entries are likewise MUTATED, never rebuilt from
scratch: the file is shared with other writers and carries per-entry state this build does
not own (baseline/peer_state overlays, fields a newer rebar wrote), so unknown and legacy
keys must survive every transition.

Deliberately NOT here: absence bookkeeping, retirement, tombstones, comment identity, GET
rotation and recovery. Those stay with ``BindingStore``, which remains the FACADE and the
only public door to binding state — and which is also where the ``note_absent_or_rekey``
coordinator lives: it performs the by-id client lookup, asks :meth:`rekey` to swap the
indexes, and falls through to its own absence path on a negative answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rebar_reconciler.timeutil import utc_now_iso

if TYPE_CHECKING:
    from rebar_reconciler.binding_repository import BindingRepository

__all__ = ["BindingLifecycle"]


def _now_iso() -> str:
    # Canonical Z-suffix UTC (twin of rebar.timeutils.utc_now_iso); the local spelling
    # used across this module's call sites, matching ``binding_store``'s.
    return utc_now_iso()


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
        """The issue's IMMUTABLE numeric Jira id for a binding, or None.

        A Jira issue's KEY changes when the issue is MOVED between projects; its
        numeric ``id`` never does. None is VALID and means "not captured yet" — every
        binding written before bug 7c26 has no id, and gets none until the next create
        re-records one. The absence path degrades to its pre-7c26 behaviour there (the
        facade's ``note_absent_or_rekey`` never reaches :meth:`rekey`), so no migration
        is required.
        """
        entry = self._bindings.get(local_id)
        if entry is None:
            return None
        jira_id = entry.get("jira_id")
        return str(jira_id) if jira_id else None

    def record_jira_id(self, local_id: str, jira_id: str) -> None:
        """Capture the immutable numeric id on an EXISTING binding (bug 7c26).

        A separate method rather than a parameter on :meth:`bind_confirm` /
        :meth:`record_pending_key`: this store is SHARED WITH CLOUD and those methods
        sit on the live write-ahead path, so keeping their signatures untouched
        makes the capture purely additive. In-memory until ``save()`` (the
        caller persists it with the same write that records the key). A no-op for
        an unbound local id, an empty id, or a re-record of the same value.
        """
        entry = self._bindings.get(local_id)
        if entry is None or not jira_id:
            return
        if entry.get("jira_id") == str(jira_id):
            return
        entry["jira_id"] = str(jira_id)
        entry["updated_at"] = _now_iso()

    def rekey(self, jira_key: str, current_key: str) -> bool:
        """Re-key a MOVED issue: swap both indexes to ``current_key`` and reset absence.

        Step 4 of the ``note_absent_or_rekey`` seam (bug 7c26). The facade keeps the
        by-id client lookup and the absence fall-through; this owns the mutation, plus
        the identity comparison that decides whether there is one to make.

        ``current_key`` is the answer to "what key does this issue's immutable numeric id
        resolve to NOW?". An EMPTY answer means the lookup could not prove anything (no
        such member on the client, a raise, a genuine-gone 404, a payload with no string
        key) and an UNCHANGED answer means nothing moved — both are declined here so the
        caller falls through to the unchanged absence bookkeeping. The recovery can only
        ADD a save, never skip an absence it did not disprove; that is what makes it safe
        on the shared Cloud path and on an unmigrated store.

        On a genuinely different key: the forward entry takes the new key, the accrued
        ``absent_404_count`` is reset (re-keying PROVES the issue is alive, and a move
        noticed after two 404s must not leave the binding one miss from retirement), and
        the reverse index is swapped in the SAME operation — otherwise the old key would
        keep resolving to this local id and re-detach the pair on the next pass. The
        entry is mutated, so unknown and legacy fields on it survive.

        Persists immediately and emits a deduped ``binding-rekeyed`` alert: an in-memory
        re-key would be undone by the next pass's load, and a silent one is
        indistinguishable from a deletion that was never noticed.

        Returns True when the binding was re-keyed, False when nothing was changed.
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
