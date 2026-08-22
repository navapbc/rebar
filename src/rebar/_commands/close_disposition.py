"""Attestation for a bug closed as a DISPOSITION rather than as completed work (bug 738a).

THE DEFECT THIS CLOSES. Two individually-correct rules composed into a trap. The close path
exempts a `duplicate` / `not_a_bug` / `escalated` bug that names a live replacement from completion
verification — correct, because such a bug never claimed to implement its own acceptance criteria,
so verifying its completion is meaningless and billable. But the certification path
(:func:`rebar.llm.completion.child_closure_findings`) requires a valid ``completion-verifier``
attestation on EVERY closed direct child, with no disposition branch — so an exempt child counted as
uncertified and withheld its parent's signature.

The composition had no honest exit. Reopening and re-closing took the same exemption every time;
``--force`` produced exactly the uncertified state the operator ruling prohibits. Measured
across the store when this was filed, the exemption keyed on the very link that documents the
disposition:

    close_class in {duplicate, not_a_bug, escalated}
      WITH a live replacement link   ->  7 of 7 UNSIGNED
      WITHOUT one                    ->  7 CERTIFIED

So certification was reachable precisely for the WORSE-documented duplicates: record what supersedes
you and forfeit it, stay silent and get signed. The only way to certify an honestly-linked duplicate
was to stop declaring it a duplicate.

THE FIX, per the operator's ruling: a disposition close now MINTS an attestation instead of
withholding one. It is signed deterministically — no LLM call, because there is still nothing to
verify — and the manifest says what it actually attests: that this ticket was closed as a named
disposition against a live replacement, NOT that its acceptance criteria were implemented. An
attestation that claimed a completion PASS would be a lie in the signed bytes, which is worse than
the gap it closes.

WHY A SEPARATE MODULE. The close gate was at its module-size ceiling when this was written, and
keeping the new logic out of it meant the call site (now in ``close_precheck.py``, after bug 74a3
split the precheck cluster out) changes by two lines.
"""

from __future__ import annotations

import os

#: Close classes that are DISPOSITIONS — a statement about where the work lives (or why it
#: will not happen), not a claim that this ticket's acceptance criteria were implemented.
#: ``close_precheck._NON_COMPLETION_BUG_CLASSES`` is an ALIAS of this object (story 111a),
#: not a second frozenset kept in sync by an equality assertion — the two cannot drift apart
#: because there is only one of them.
DISPOSITION_CLASSES = frozenset(
    {"duplicate", "not_a_bug", "escalated", "obsolete", "superseded", "wontfix"}
)

#: The ADMINISTRATIVE subset (ticket fc20): the only ``--class`` values a NON-BUG close may
#: carry. ``not_a_bug`` / ``escalated`` stay bug-only vocabulary — a task/story/epic never
#: "behaves as intended" or "escalates a defect". Enforced write-side in
#: :func:`rebar._commands.txn.close_class_refusal`, gate-independent.
ADMINISTRATIVE_CLASSES = frozenset({"duplicate", "obsolete", "superseded", "wontfix"})

#: Classes whose close must carry the operator's ``--reason`` text as its justification: it is
#: REQUIRED at write time, persists as ``close_reason`` on the close STATUS event, and is what
#: the disposition attestation signs. Two shapes share the requirement: the administrative pair
#: (``obsolete``/``wontfix``, ticket fc20) carries no replacement link at all, and the bug-only
#: pair (``not_a_bug``/``escalated``, bug d54b) may substitute a live replacement link for the
#: reason — see :data:`REPLACEMENT_SATISFIES_REASON_CLASSES`. Before d54b the bug-only pair fell
#: through to FULL completion verification when unlinked, which demands proof a nonexistent (or
#: out-of-tracker) defect was fixed — an unpassable gate that forced operators to ``--force``.
REASON_REQUIRED_CLASSES = frozenset({"obsolete", "wontfix", "not_a_bug", "escalated"})

#: The reason-required classes for which a LIVE REPLACEMENT LINK satisfies the requirement
#: instead of ``--reason`` (bug d54b). The replacement is checked FIRST, so the pre-d54b linked
#: path is preserved unchanged; the reason mint is the fallback for the close that names nothing
#: in-tracker (``not_a_bug``: the RCA proved no defect exists; ``escalated``: the work went to
#: another owner/system — the reason names where). DELIBERATE decision recorded on ticket d54b:
#: ``escalated`` is reason-required the same way as ``not_a_bug`` because the structural problem
#: is identical — no defect is fixed in-repo, so completion verification is unpassable — while
#: replacement-preferred behavior is kept by the check order. ``obsolete``/``wontfix`` are
#: excluded on purpose: their justification IS the reason, and a stray link must not substitute.
REPLACEMENT_SATISFIES_REASON_CLASSES = frozenset({"not_a_bug", "escalated"})


# replacement-walk-ok: this function is the SINGLE owner of the duplicates/supersedes walk
# (story 111a-4626-8d2c-42bf). Every other caller in the tree is a thin wrapper over it.
def replacement_of(
    ticket_id: str,
    tracker: str,
    *,
    require_live: bool,
    on_subject_unreadable: str = "stop",
) -> str | None:
    """The ticket that replaces ``ticket_id``, or ``None`` — the ONE replacement-link walk.

    Three near-identical copies of this walk used to live in ``close_precheck`` and here, and
    bug ``frolicky-dependable-peccary`` came from exactly that duplication: ``verdict()``
    consulted one copy while the rule it needed lived in another. Only what was genuinely the
    same is collapsed — the WALK and the liveness mode. Class and ``ticket_type`` filtering
    stays in each wrapper, because the three differ there and merging those guards would
    silently change which closes are exempt.

    Replacement relationships are DIRECTIONAL, not symmetric: either this ticket ``duplicates``
    a canonical one (an outbound ``deps`` entry), or another ticket ``supersedes`` this one (an
    inbound link). Reduced state and the inbound reader both expose only net-active links,
    including links baked into snapshots. The outbound pass wins when both exist.

    ``require_live=True`` asks "is there a USABLE replacement?", filtering BOTH directions
    through :func:`close_precheck._is_live_ticket`; ``False`` asks the strictly weaker "was one
    ever RECORDED?", which bug 9b70's remedy needs — a link naming an archived target earns a
    different message from having recorded nothing at all.

    ``on_subject_unreadable`` fixes the posture when ``reduce_ticket`` on the SUBJECT fails or
    returns a non-dict. ``"stop"`` returns ``None`` at once — the FAIL-CLOSED posture the two
    live-mode callers depend on, since they gate a bypass and must not fall through to the
    inbound pass and find a live ``supersedes`` source there. ``"continue"`` proceeds anyway,
    for the reader that only picks between two remedy strings.
    """
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
    """The live ticket this disposition points AT, or ``None``.

    Returns the id rather than a bool so it can be named in the signed manifest — an
    attestation that says "closed as a duplicate" without saying OF WHAT is not auditable.

    Guards on :data:`DISPOSITION_CLASSES` only and takes NO ``ticket_type``, making it strictly
    MORE permissive for non-bug tickets than ``close_precheck._has_live_replacement_link``'s
    extra ``ADMINISTRATIVE_CLASSES`` narrowing (ticket fc20). That difference is deliberate and
    lives here, not in :func:`replacement_of`. Fail-closed on any unreadable state.
    """
    if close_class not in DISPOSITION_CLASSES:
        return None
    return replacement_of(ticket_id, tracker, require_live=True, on_subject_unreadable="stop")


def _mint(close_class: str, **evidence: str) -> dict:
    """A deterministic disposition sign signal carrying ``evidence`` (replacement or reason)."""
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
    """The sign signal for a disposition close, or ``None`` if it does not qualify.

    Shaped like the completion verifier's PASS ``result`` so the existing signing path
    (``sign_completion_verdict`` -> ``_verdict_manifest`` -> ``signing.sign_manifest``) mints it
    with no special-casing at the call site. ``model``/``runner`` are recorded as the deterministic
    producer rather than left as ``n/a``, so an auditor reading the manifest can tell at a glance
    that no model was consulted — which is the honest description of what happened.

    Three doors, in precedence order. A :data:`REPLACEMENT_SATISFIES_REASON_CLASSES` close checks
    its replacement link FIRST (the pre-d54b mint, preserved unchanged — a linked ``not_a_bug``/
    ``escalated`` attests the replacement even when a reason was also given), falling back to the
    reason mint. A purely reason-required administrative class (obsolete/wontfix, ticket fc20) is
    attested from ``(class, close_reason)`` only — it names no replacement, so an empty reason
    yields ``None`` (fail-closed: an unjustified disposition must not sign). Every other
    disposition class keeps the replacement-bearing mint from :func:`find_replacement`.
    """
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
    """Rewrite a completion manifest to state that it attests a DISPOSITION.

    The first line becomes ``completion-verifier: DISPOSITION <class>`` instead of
    ``completion-verifier: PASS``, and the replacement is named. The ``kind`` a reader derives from
    the manifest is the text before the first ``": "`` (``reducer/_processors_identity.py``), so the
    record is still a ``completion-verifier`` attestation and
    ``verify_signature(kind="completion-verifier")`` finds it — while the signed bytes no longer
    claim a completion PASS that never happened.

    Every other step (ticket, model, runner, rebar version, material fingerprint) is preserved
    untouched, so validity-on-read — including the material-drift check that makes a post-verdict
    edit invalidate the attestation — behaves identically to a verified completion.
    """
    disposition = result.get("disposition")
    if not disposition:
        return manifest
    if not manifest:
        # Defensive, and it matters: rewriting `decorated[0]` on an empty list raises IndexError,
        # which inside the close path would surface as a crash rather than as an unsigned close.
        # `_verdict_manifest` always yields several steps today, so this is unreachable from that
        # caller — but this helper takes a caller-supplied list, and a signing helper that can raise
        # on a degenerate input is a worse failure mode than one that declines to decorate.
        return manifest
    decorated = list(manifest)
    decorated[0] = f"completion-verifier: DISPOSITION {disposition}"
    replacement = result.get("replacement")
    if replacement:
        # Inserted right after the first line so the manifest reads as a sentence, and BEFORE the
        # material/sha steps so their positions stay at the end where readers expect them.
        decorated.insert(1, f"replacement: {replacement}")
    close_reason = result.get("close_reason")
    if close_reason:
        # The reason-only mint (ticket fc20): the signed bytes carry the operator's stated
        # justification, because for obsolete/wontfix that reason IS the whole disposition —
        # there is no replacement ticket to name.
        decorated.insert(1, f"disposition: {disposition} reason: {close_reason}")
    return decorated
