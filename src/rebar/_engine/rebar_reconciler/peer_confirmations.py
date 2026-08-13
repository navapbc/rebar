"""Durable per-link PEER-CONFIRMATION records (epic a4bd, story 248f).

WHY THIS EXISTS. ``managed_refs`` (``rebar.reducer._managed_refs``) marks a ref
managed the instant it is created LOCALLY, and is strictly monotonic. So the
inbound removal path's managed check (G3) proves "we own this ref", NOT "we ever
pushed it to the peer" — a brand-new never-pushed local link and a link the peer
genuinely deleted are state-indistinguishable. G4 (the same-pass outbound-ADD
suppression) covers that blind spot only when the outbound differ emits the
re-ADD, and ``outbound_links._diff_links`` dedups ADDs direction-agnostically on
``(vendor_type, target_key)`` — "intentionally NOT deduped on relation" — so a
local link whose vendor type collides with a remote link in the OTHER direction
is never pushed AND never G4-protected.

This store is the missing discriminator: it records, per link, the EVIDENCE that
the peer has actually seen it. The rule the epic fixes is that absence may delete
only something previously proven synchronized; an unqualified missing link is
never deletion evidence.

WHY A FULL RECORD, NOT A BOOLEAN. A boolean cannot say which side created the
link, which vendor link id it carries, or which read confirmed it — and the
completeness tracking the snapshot path needs (a partial read is NON-evidence) is
impossible without that provenance. Jira exposes deletion as an explicit event
keyed by a stable link id, so retaining the id keeps the evidence strong enough
for bidirectional reconciliation.

WHY A ``.bridge_state`` SIDECAR AND NOT REDUCER STATE. The record carries the
VENDOR link id and is peer-derived, whereas ``_managed_refs`` is deliberately
provider-agnostic (LOCAL ids only, so the primitive is reusable for Linear or
GitHub). Putting vendor ids into ticket ``compiled_state`` would break that and
would emit a ticket event — auto-committed and auto-pushed to the ``tickets``
branch — on every outbound link push. Every other peer-derived fact (binding
baselines, ``peer_parent``) already lives in ``.bridge_state``. Ticket-log
SNAPSHOT compaction cannot touch a sidecar, so the record survives it trivially.

KEYED ON LOCAL IDS, RELATION-SCOPED. ``(source_local_id, target_local_id,
relation)`` matches the reader — ``apply_inbound_records._inbound_unlink_one``,
relation-scoped since e39f — and ``impossible_links.record_key``. The outbound
ADD dedup key ``(vendor_type, target_key)`` is deliberately NOT used: it is
direction-agnostic and not relation-keyed, so it cannot express the granularity
the removal decision needs.

FAIL-OPEN, NEVER FAIL-CLOSED. A corrupt or absent store degrades to "nothing
confirmed" rather than raising, matching ``bindings-retired.json`` and NOT
``bindings.json``. Losing the evidence must never break a sync pass.
"""

from __future__ import annotations

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
    """Read/modify/write access to the durable peer-confirmation records.

    Loads eagerly and writes only when something actually changed, so a
    converged pass touches the file zero times. A corrupt or unreadable store
    degrades to empty rather than raising (see the module docstring): the
    evidence is a safety optimisation, never a precondition for running a pass.
    """

    def __init__(self, tracker_dir: str) -> None:
        self.tracker_dir = str(tracker_dir)
        self.path = os.path.join(self.tracker_dir, STORE_RELATIVE)
        self._records: dict[str, dict[str, Any]] = {}
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
        pass_id: str | None = None,
        source_kind: str = SOURCE_PUSH,
    ) -> None:
        """Record that the peer carries this link, with the evidence that proves it.

        ``link_id`` is the vendor's stable link id when the transport supplies
        one. Backends may legitimately return none; a missing id is stored as
        ``None`` and is NOT a failure — the record still proves synchronization.
        """
        key = record_key(source_id, target_id, relation)
        now = time.time_ns()
        previous = self._records.get(key) or {}
        self._records[key] = {
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation,
            "link_id": link_id,
            "direction": direction,
            "source": source_kind,
            "confirmed_pass": pass_id,
            "confirmed_at": now,
            "first_confirmed_at": previous.get("first_confirmed_at", now),
        }
        self._dirty = True

    def save(self) -> None:
        """Persist the records if anything changed (atomic replace)."""
        if not self._dirty:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = {"version": SCHEMA_VERSION, "records": self._records}
        tmp_path = f"{self.path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            os.replace(tmp_path, self.path)
        except OSError as exc:
            logger.warning("peer_confirmations: could not persist %s (%r)", self.path, exc)
            return
        self._dirty = False


def confirm_from_snapshot(
    store: PeerConfirmationStore,
    curr_snapshot: Any,
    binding_store: Any,
    pass_id: str | None = None,
) -> int:
    """Confirm links OBSERVED in an authoritative fetched snapshot. Returns the count.

    The second, independent evidence source: a link present on the peer is proven
    synchronized even if this clone never pushed it (peer-created links, links
    pushed before this store existed, links pushed by another clone).

    COMPLETENESS IS THE WHOLE CONTRACT. A partial, paginated, failed or stale read
    is NON-EVIDENCE — it can neither confirm nor deny. We do not invent a signal for
    that: ``fetcher.merge_issuelinks_map`` already writes the per-issue
    ``issuelinks`` key ONLY on an authoritative read, so an ABSENT key means
    unobserved (truncated page walk, HTTP 410, failed enrichment, a backend with no
    ``get_issuelinks_map``) and ``[]`` means authoritatively-empty. Hence the
    ``"issuelinks" in entry`` membership test below — ``entry.get("issuelinks") or
    []`` would collapse observed-empty into unobserved, the exact trap
    ``inbound_differ``'s G1 docstring warns against. A stalled pager never reaches
    here at all: ``BackendPaginationStallError`` re-raises rather than degrading.

    MONOTONIC AND ADDITIVE. Observing a link confirms it; NOT observing one never
    un-confirms. There is deliberately no un-confirmation path — reintroducing
    "absence is evidence" is precisely the failure this epic exists to remove.

    ``resolve_inbound_link`` is used per raw entry rather than ``observed_peer_deps``
    because the latter returns only ``(relation, target)`` and DISCARDS the vendor
    link id this record must persist.
    """
    from rebar_reconciler.link_direction import resolve_inbound_link

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
                pass_id=pass_id,
                source_kind=SOURCE_SNAPSHOT,
            )
            written += 1
    return written


def backfill_from_managed_refs(
    store: PeerConfirmationStore,
    local_tickets: Any,
    binding_store: Any,
    pass_id: str | None = None,
) -> int:
    """Grandfather pre-upgrade managed links as confirmed. Returns the count written.

    WHY THIS IS REQUIRED, not a nicety. The removal path declines any link with no
    confirmation record. On the FIRST run after this feature ships the store is
    empty, so without a backfill every legitimate peer deletion would be declined —
    a worse regression than the blind spot the epic set out to close. Grandfathering
    trades a one-time assumption for that.

    THE ASSUMPTION IS MARKED, not hidden. Records carry ``source="backfill"`` and
    ``direction="backfill"`` so an operator can always separate assumed evidence from
    proven evidence. It is deliberately NOT distinguished at the decision point —
    treating backfill as weaker there would re-open the blind spot for every
    pre-upgrade link, which is precisely what grandfathering exists to prevent.

    ONE-SHOT ON FILE ABSENCE, not on emptiness (``store.was_absent``): an operator who
    deliberately emptied the store must not have it silently repopulated next pass.

    NEVER DOWNGRADES. ``record()`` overwrites its key unconditionally, so this must
    read before writing and skip any key that already holds a record. An existing
    ``push`` or ``snapshot`` entry is strictly stronger evidence than a grandfather
    assumption, and clobbering it would lose the vendor link id and the confirming
    pass. The reverse direction needs no code: a later real confirmation overwrites a
    backfilled entry by that same unconditional behaviour, so provenance upgrades.
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
                pass_id=pass_id,
                source_kind=SOURCE_BACKFILL,
            )
            written += 1
    return written


def open_store(repo_root: Any) -> PeerConfirmationStore:
    """Open the store for ``repo_root``'s tracker dir."""
    from rebar._commands._seam import tracker_dir

    return PeerConfirmationStore(str(tracker_dir(repo_root)))
