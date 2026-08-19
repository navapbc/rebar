"""Lifecycle policy owner for binding state (RP-02 S2).

``BindingLifecycle`` owns the IDENTITY half of binding lifecycle policy: the
``pending`` → keyed-``pending`` → ``confirmed`` progression, ``unbind``'s two-index
teardown, capture of the immutable numeric Jira id (bug 7c26), and the re-key that keeps a
binding attached to an issue that MOVED project.

It also owns the ABSENCE half (RP-02 S2 T2): consecutive-404 bookkeeping and its
retirement grace (bug 1e08), the retired-first soft delete and its ``{local_id: jira_key}``
tombstone index (bug 3b5f), and the append-only comment-identity map
(emersed-specific-mutt / the DIG-5301 duplicate-comment class). The grace knob's ambient
env sourcing moved here with it, unchanged.

It is not a store and not a persistence owner. :class:`BindingRepository` owns every byte
on disk; this class mutates the repository's OWN ``bindings`` / ``reverse`` dictionaries
IN PLACE. They are reached back through the repository on every access — never copied,
never cached — because a copy at this boundary would look correct in memory and silently
discard every transition at save time. Entries are likewise MUTATED, never rebuilt from
scratch: the file is shared with other writers and carries per-entry state this build does
not own (baseline/peer_state overlays, fields a newer rebar wrote), so unknown and legacy
keys must survive every transition.

Deliberately NOT here: GET rotation and pending-binding recovery. Those stay with
``BindingStore``, which remains the FACADE and the only public door to binding state — and
which is also where the ``note_absent_or_rekey`` coordinator lives: it performs the by-id
client lookup, asks :meth:`rekey` to swap the indexes, and falls through to :meth:`note_absent`
on a negative answer.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from rebar_reconciler.timeutil import utc_now_iso

if TYPE_CHECKING:
    from rebar_reconciler.binding_repository import BindingRepository

__all__ = ["BindingLifecycle"]


def _now_iso() -> str:
    # Canonical Z-suffix UTC (twin of rebar.timeutils.utc_now_iso); the local spelling
    # used across this module's call sites, matching ``binding_store``'s.
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

    Moved here from ``binding_store`` with the absence policy it parameterizes (RP-02
    S2 T2). The SOURCING is deliberately byte-identical to the pre-move read — a direct
    ambient ``os.environ`` lookup, not a configuration-seam call — because cutting it to
    that seam is RP-04 S7.3.a's slice, not this one. Both legacy-exception rows in
    ``scripts/config_ownership_exceptions.py`` were re-registered under this module's
    path in the same change (the gate keys them on path + symbol).
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
        grace = _env_int(
            "RECONCILER_ABSENT_RETIRE_GRACE",
            _DEFAULT_ABSENT_RETIRE_GRACE,
            minimum=1,
        )
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
        """The retired ``jira_key`` this local ticket was last bound to, or ``None``.

        The reverse of :meth:`is_retired`, which is keyed by ``jira_key`` and therefore
        cannot answer the question the unbound-create arm needs to ask: retirement
        UNBINDS the local ticket (:meth:`retire` pops it from both ``bindings`` and
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

        Both indexes are lifted together: the retired key set and every
        ``retired_locals`` entry pointing at this key, or the local side would keep
        reporting a tombstone the file no longer holds.

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

    # -- comment-ID map (append-only comment sync; emersed-specific-mutt) ---

    def record_comment_id(self, local_comment_key: str, jira_comment_id: str) -> None:
        """Persist a COMMENT-HLC-key -> Jira-comment-ID pairing and persist NOW.

        The map identifies an already-mirrored comment by the COMMENT event's HLC
        ``timestamp`` (a stable, unique ``local_comment_key``), not by body — so
        same-text comments never collide and an edited body is not seen as new.
        Unlike :meth:`record_jira_id`, this saves IMMEDIATELY (write-ahead):
        it is called on the successful ``add_comment`` return, and the durable
        entry is what the outbound differ's PRIMARY skip keys on, so a crash after
        the Jira post cannot re-post (closes the DIG-5301 duplicate class).

        Change-gated and idempotent: an identical re-record is a no-op, with no save
        churn. The gate is equality-only, so a DIFFERING id for a known key overwrites
        it and saves. The reviewed base's docstring claimed a key "is never remapped to
        a different id"; its code did not do that, so the claim is corrected here rather
        than carried forward — the behaviour is unchanged and pinned by
        ``test_a_differing_id_for_a_known_key_overwrites_and_saves``.

        The map is created with ``setdefault``, so a legacy store written before comment
        sync existed is readable without materializing the key — reading never rewrites
        the file.
        """
        key = str(local_comment_key)
        comment_ids = self._data.setdefault("comment_ids", {})
        if comment_ids.get(key) == str(jira_comment_id):
            return
        comment_ids[key] = str(jira_comment_id)
        self._repo.save()

    def comment_id_for(self, local_comment_key: str) -> str | None:
        """The recorded Jira comment ID for an HLC key, or ``None`` when unmapped."""
        recorded: str | None = self._data.get("comment_ids", {}).get(str(local_comment_key))
        return recorded

    def is_comment_mapped(self, local_comment_key: str) -> bool:
        """True once :meth:`record_comment_id` has persisted this HLC key."""
        return str(local_comment_key) in self._data.get("comment_ids", {})
