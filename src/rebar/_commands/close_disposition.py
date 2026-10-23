"""Deterministic attestations for disposition closes.
A linked replacement or recorded reason can certify why work closed
without claiming its acceptance criteria were implemented. The signed
manifest says DISPOSITION and retains the completion-verifier kind."""

from __future__ import annotations

import os

#: Disposition classes describe where work went or why it ended.
#: close_precheck aliases this same set.
DISPOSITION_CLASSES = frozenset(
    {"duplicate", "not_a_bug", "escalated", "obsolete", "superseded", "wontfix"}
)

#: Only these classes apply to non-bug tickets.
ADMINISTRATIVE_CLASSES = frozenset({"duplicate", "obsolete", "superseded", "wontfix"})

#: These classes require a recorded reason, except that not_a_bug and
#: escalated may name a usable replacement instead.
REASON_REQUIRED_CLASSES = frozenset({"obsolete", "wontfix", "not_a_bug", "escalated"})

#: Prefer a usable replacement for these bug classes, then fall back to
#: the reason. Obsolete and wontfix still require the stated reason.
REPLACEMENT_SATISFIES_REASON_CLASSES = frozenset({"not_a_bug", "escalated"})


# replacement-walk-ok: Single owner of the duplicates and supersedes walk.
def replacement_of(
    ticket_id: str,
    tracker: str,
    *,
    require_live: bool,
    on_subject_unreadable: str = "stop",
) -> str | None:
    """Find a recorded or usable replacement for a ticket.
    Prefer its outbound duplicates link, then an inbound supersedes link.
    Both directions use net-active links, including snapshots. require_live
    filters either direction for a usable target. On unreadable subject state,
    stop for fail-closed usable-target checks or continue for remedy wording."""
    from rebar._commands.close_precheck import _is_live_ticket
    from rebar.reducer import reduce_ticket

    def _usable(candidate: str) -> bool:
        return bool(candidate) and (not require_live or _is_live_ticket(candidate, tracker))

    try:
        state = reduce_ticket(os.path.join(tracker, ticket_id))
    except Exception:  # noqa: BLE001 — an unreadable source never fails toward a bypass
        state = None
    if not isinstance(state, dict):
        if on_subject_unreadable == "stop":
            return None
        state = {}

    for dep in state.get("deps") or []:
        if dep.get("relation") != "duplicates":
            continue
        target = str(dep.get("target_id", dep.get("target", "")) or "")
        if _usable(target):
            return target

    from rebar.reducer._inbound import find_inbound_relationships

    try:
        inbound = find_inbound_relationships(ticket_id, tracker)
    except Exception:  # noqa: BLE001 — a failed graph read must retain fail-closed verification
        return None
    for link in inbound.get("inbound_links") or []:
        if link.get("relation") != "supersedes":
            continue
        source = str(link.get("from_id") or "")
        if _usable(source):
            return source
    return None


def find_replacement(ticket_id: str, close_class: str, tracker: str) -> str | None:
    """Return a usable replacement ID for a disposition class, or None.
    This checks disposition classes without ticket-type narrowing and fails
    closed on unreadable state."""
    if close_class not in DISPOSITION_CLASSES:
        return None
    return replacement_of(ticket_id, tracker, require_live=True, on_subject_unreadable="stop")


def _mint(close_class: str, **evidence: str) -> dict:
    """Build a deterministic disposition sign signal with evidence."""
    return {
        "verdict": "PASS",
        "disposition": close_class,
        **evidence,
        "model": "none (deterministic disposition)",
        "runner": "close_disposition",
        "findings": [],
    }


def verdict(
    ticket_id: str, close_class: str, tracker: str, *, close_reason: str = ""
) -> dict | None:
    """Return a disposition sign signal or None.
    For not_a_bug and escalated, prefer a usable replacement, then a reason.
    Obsolete and wontfix require a reason. Other disposition classes use
    the replacement. The result names its deterministic producer so an
    auditor can tell no model verified completion."""
    if close_class in REASON_REQUIRED_CLASSES:
        if close_class in REPLACEMENT_SATISFIES_REASON_CLASSES:
            replacement = find_replacement(ticket_id, close_class, tracker)
            if replacement is not None:
                return _mint(close_class, replacement=replacement)
        if not close_reason:
            return None
        return _mint(close_class, close_reason=close_reason)
    replacement = find_replacement(ticket_id, close_class, tracker)
    if replacement is None:
        return None
    return _mint(close_class, replacement=replacement)


def reason_refusal(close_class: str, ticket_id: str = "", tracker: str = "") -> str | None:
    """The write-side refusal for a reason-required close missing its ``--reason``, or ``None``.

    Shared vocabulary logic for :func:`rebar._commands.txn.close_class_refusal` (bug d54b): a
    :data:`REPLACEMENT_SATISFIES_REASON_CLASSES` close is exempt when a live replacement link
    exists (checked only when the caller can name the store — a missing ``ticket_id``/``tracker``
    fails toward requiring the reason, never toward waving the close through), and its refusal
    names BOTH doors so the operator is told exactly which flag to add instead of falling through
    to completion verification, which is unpassable for these classes.
    """
    if close_class in REPLACEMENT_SATISFIES_REASON_CLASSES:
        if ticket_id and tracker and find_replacement(ticket_id, close_class, tracker):
            return None
        what = (
            "why no defect exists"
            if close_class == "not_a_bug"
            else "where the work was escalated to"
        )
        return (
            f"--class {close_class} requires --reason=<text> stating {what} (recorded as "
            "close_reason on the close event and signed into the disposition attestation), "
            "or a live replacement link (an outbound 'duplicates' or inbound 'supersedes' "
            "relation), which satisfies it instead"
        )
    return (
        f"--class {close_class} requires --reason=<text> stating why: the reason is "
        "recorded as close_reason on the close event and signed into the disposition "
        "attestation"
    )


def decorate_manifest(manifest: list[str], result: dict) -> list[str]:
    """Mark a signed manifest as a DISPOSITION, naming its replacement or
    reason. Preserve completion-verifier as the attestation kind and keep
    material fingerprints so later edits still invalidate the signature."""
    disposition = result.get("disposition")
    if not disposition:
        return manifest
    if not manifest:
        # Return an empty input unchanged instead of raising during close.
        return manifest
    decorated = list(manifest)
    decorated[0] = f"completion-verifier: DISPOSITION {disposition}"
    replacement = result.get("replacement")
    if replacement:
        # Name the replacement near the header, before material metadata.
        decorated.insert(1, f"replacement: {replacement}")
    close_reason = result.get("close_reason")
    if close_reason:
        # Sign the operator's reason when no replacement is named.
        decorated.insert(1, f"disposition: {disposition} reason: {close_reason}")
    return decorated
