"""Durable evidence that a peer has seen each relation-scoped link.

``managed_refs`` proves local ownership, not peer delivery. Removal requires
a prior push, authoritative snapshot, or marked upgrade backfill; absence alone
is not deletion evidence. Records retain source, vendor link ID, and provenance,
keyed by ordered local IDs and relation. Peer-derived data lives in
``.bridge_state`` so reducer state stays provider-agnostic and ticket events do
not churn. Missing or corrupt evidence degrades to unconfirmed without stopping
the sync pass.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Path of the store relative to the tracker dir. Single-sourced against
#: ``git_adapter.PEER_CONFIRMATIONS_FILE``, which is what the commit-back helper
#: stages; the two are pinned together by test.
STORE_RELATIVE = os.path.join(".bridge_state", "peer_confirmations.json")

#: We pushed the link and the vendor accepted the write.
DIRECTION_OUTBOUND = "outbound"
#: We observed the link in an authoritative fetched snapshot.
DIRECTION_SNAPSHOT = "snapshot"
#: Grandfathered at upgrade — assumed, never observed. Kept in the SAME closed
#: vocabulary as the two evidence-backed directions so an operator reading the file
#: can always tell an assumption from a proof.
DIRECTION_BACKFILL = "backfill"

#: Provenance of a record, most-to-least direct.
SOURCE_PUSH = "push"
SOURCE_SNAPSHOT = "snapshot"
SOURCE_BACKFILL = "backfill"


def record_key(source_id: str, target_id: str, relation: str) -> str:
    """The store key for one ordered, relation-qualified link triple."""
    return f"{source_id}|{target_id}|{relation}"


class PeerConfirmationStore:
    """Load peer evidence eagerly and write only changed records.

    Corrupt or unreadable evidence degrades to an empty store, keeping the
    sync pass available.
    """

    def __init__(self, tracker_dir: str) -> None:
        self.tracker_dir = str(tracker_dir)
        self.path = os.path.join(self.tracker_dir, STORE_RELATIVE)
        self._records: dict[str, dict[str, Any]] = {}
        self._baseline_records: dict[str, dict[str, Any]] = {}
        self._dirty = False
        # Did the store FILE not exist when we opened it? The one-shot upgrade
        # backfill keys off this rather than off emptiness: an operator who
        # deliberately emptied the store must not have it silently repopulated.
        self.was_absent = False
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            self.was_absent = True
            return
        except (OSError, ValueError) as exc:
            # Corrupt / unreadable / conflict-markered: degrade to empty. The
            # cost is declining nothing we would otherwise decline (fail-open),
            # which is strictly the pre-feature behaviour.
            logger.warning(
                "peer_confirmations: unreadable store at %s (%r); treating as empty",
                self.path,
                exc,
            )
            return
        records = data.get("records") if isinstance(data, dict) else None
        if isinstance(records, dict):
            self._records = {
                key: value for key, value in records.items() if isinstance(value, dict)
            }
        self._baseline_records = copy.deepcopy(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def get(self, source_id: str, target_id: str, relation: str) -> dict[str, Any] | None:
        """The stored record for a triple, or ``None``."""
        return self._records.get(record_key(source_id, target_id, relation))

    def is_confirmed(self, source_id: str, target_id: str, relation: str) -> bool:
        """Has the peer been PROVEN to carry this exact link?

        Relation-scoped by construction: the same ordered pair under a different
        relation is a different key and is NOT confirmed by this one.
        """
        return self.get(source_id, target_id, relation) is not None

    def record(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        *,
        link_id: str | None = None,
        direction: str = DIRECTION_OUTBOUND,
        source_kind: str = SOURCE_PUSH,
    ) -> None:
        """Record material confirmation evidence without per-pass churn.

        A backend may omit its stable ``link_id`` without invalidating the
        confirmation. ``first_confirmed_at`` is preserved; identical evidence
        does not dirty or rewrite the sidecar.
        """
        key = record_key(source_id, target_id, relation)
        previous = self._records.get(key) or {}
        candidate = {
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation,
            "link_id": link_id,
            "direction": direction,
            "source": source_kind,
            "first_confirmed_at": previous.get("first_confirmed_at", time.time_ns()),
        }
        if candidate == previous:
            return  # unchanged evidence: no rewrite (0303 churn discipline)
        self._records[key] = candidate
        self._dirty = True

    def save(self) -> None:
        """Persist the records if anything changed (atomic replace)."""
        if not self._dirty:
            return
        from rebar.reconciler.sidecar_transactions import persist_record_deltas

        if persist_record_deltas(
            self.path,
            version=SCHEMA_VERSION,
            baseline_records=self._baseline_records,
            desired_records=self._records,
            log_label="peer_confirmations",
        ):
            self._baseline_records = copy.deepcopy(self._records)
            self._dirty = False


def confirm_from_snapshot(
    store: PeerConfirmationStore,
    curr_snapshot: Any,
    binding_store: Any,
) -> int:
    """Confirm observed links from authoritative snapshots; return their count.

    Only an entry with an ``issuelinks`` key is observed; an absent key marks an
    incomplete read, while ``[]`` is authoritatively empty. Confirmation is
    additive and never revoked by absence. Resolve raw links to retain vendor
    IDs that ``observed_peer_deps`` discards.
    """
    from rebar.reconciler.link_direction import resolve_inbound_link

    written = 0
    for jira_key, entry in (curr_snapshot or {}).items():
        if not isinstance(entry, dict) or "issuelinks" not in entry:
            continue  # UNOBSERVED — never evidence, in either direction
        links = entry.get("issuelinks")
        if not isinstance(links, list):
            continue
        source_local_id = binding_store.get_local_id(jira_key)
        if not source_local_id:
            continue  # unbound source: nothing local to key the evidence on
        for link in links:
            if not isinstance(link, dict):
                continue
            other_key, relation = resolve_inbound_link(link)
            if not other_key or not relation:
                continue  # unmapped vendor link type or malformed entry
            target_local_id = binding_store.get_local_id(other_key)
            if not target_local_id:
                continue
            store.record(
                str(source_local_id),
                str(target_local_id),
                str(relation),
                link_id=link.get("id"),
                direction=DIRECTION_SNAPSHOT,
                source_kind=SOURCE_SNAPSHOT,
            )
            written += 1
    return written


def backfill_from_managed_refs(
    store: PeerConfirmationStore,
    local_tickets: Any,
    binding_store: Any,
) -> int:
    """Grandfather managed links once when the sidecar file is absent.

    Mark assumptions as ``backfill`` so they remain distinguishable from push
    or snapshot evidence. An empty existing file does not trigger backfill, and
    existing stronger records are never overwritten. Later real confirmation
    may replace an assumed record.
    """
    from rebar.reducer._managed_refs import managed_ref_set

    if not store.was_absent:
        return 0

    written = 0
    for ticket in local_tickets or []:
        if not isinstance(ticket, dict):
            continue
        local_id = ticket.get("ticket_id") or ticket.get("id")
        if not local_id:
            continue
        for kind, target in sorted(managed_ref_set(ticket)):
            if not binding_store.get_jira_key(target):
                continue  # unbound target: no peer link could exist to grandfather
            if store.get(str(local_id), str(target), str(kind)) is not None:
                continue  # already proven — never downgrade to an assumption
            store.record(
                str(local_id),
                str(target),
                str(kind),
                link_id=None,
                direction=DIRECTION_BACKFILL,
                source_kind=SOURCE_BACKFILL,
            )
            written += 1
    return written


def open_store_or_none(repo_root: Any) -> PeerConfirmationStore | None:
    """Open the store, or return ``None`` to retain pre-confirmation removal behavior.

    Unreadable sidecar evidence must not halt inbound convergence.
    """
    try:
        return open_store(repo_root)
    except Exception as exc:  # noqa: BLE001 — fail-open: no evidence is worse, not fatal
        logger.debug("peer_confirmations: store unavailable: %r", exc)
        return None


def open_store(repo_root: Any) -> PeerConfirmationStore:
    """Open the store for ``repo_root``'s tracker dir."""
    from rebar._commands._seam import tracker_dir

    return PeerConfirmationStore(str(tracker_dir(repo_root)))
