"""Attestation for a bug closed as a DISPOSITION rather than as completed work (bug 738a).

THE DEFECT THIS CLOSES. Two individually-correct rules composed into a trap. The close path
exempts a `duplicate` / `not_a_bug` / `escalated` bug that names a live replacement from completion
verification — correct, because such a bug never claimed to implement its own acceptance criteria,
so verifying its completion is meaningless and billable. But the certification path
(:func:`rebar.llm.completion.child_closure_findings`) requires a valid ``completion-verifier``
attestation on EVERY closed direct child, with no disposition branch — so an exempt child counted as
uncertified and withheld its parent's signature.

The composition had no honest exit. Reopening and re-closing took the same exemption every time;
``--force-close`` produced exactly the uncertified state the operator ruling prohibits. Measured
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

#: Bug close classes that are DISPOSITIONS — a statement about where the work lives, not a claim
#: that this ticket's acceptance criteria were implemented. Mirrors
#: ``close_precheck._NON_COMPLETION_BUG_CLASSES``; the two are asserted equal by
#: ``tests/unit/test_close_disposition_attestation_738a.py`` so they cannot drift apart.
DISPOSITION_CLASSES = frozenset({"duplicate", "not_a_bug", "escalated"})


def find_replacement(ticket_id: str, close_class: str, tracker: str) -> str | None:
    """The live ticket this disposition points AT, or ``None``.

    Directional, exactly as ``close_precheck._has_live_replacement_link`` is: either this bug
    ``duplicates`` a canonical ticket, or another ticket ``supersedes`` this bug. Returns the id
    rather than a bool so it can be named in the signed manifest — an attestation that says "closed
    as a duplicate" without saying OF WHAT is not auditable.

    Fail-closed on any unreadable state: ``None`` means the caller keeps normal verification.
    """
    if close_class not in DISPOSITION_CLASSES:
        return None

    from rebar._commands.close_precheck import _is_live_ticket
    from rebar.reducer import reduce_ticket

    try:
        state = reduce_ticket(os.path.join(tracker, ticket_id))
    except Exception:  # noqa: BLE001 — an unreadable source must retain fail-closed verification
        return None
    if not isinstance(state, dict):
        return None

    for dep in state.get("deps") or []:
        if dep.get("relation") != "duplicates":
            continue
        target = str(dep.get("target_id", dep.get("target", "")) or "")
        if target and _is_live_ticket(target, tracker):
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
        if source and _is_live_ticket(source, tracker):
            return source
    return None


def verdict(ticket_id: str, close_class: str, tracker: str) -> dict | None:
    """The sign signal for a disposition close, or ``None`` if it does not qualify.

    Shaped like the completion verifier's PASS ``result`` so the existing signing path
    (``sign_completion_verdict`` -> ``_verdict_manifest`` -> ``signing.sign_manifest``) mints it
    with no special-casing at the call site. ``model``/``runner`` are recorded as the deterministic
    producer rather than left as ``n/a``, so an auditor reading the manifest can tell at a glance
    that no model was consulted — which is the honest description of what happened.
    """
    replacement = find_replacement(ticket_id, close_class, tracker)
    if replacement is None:
        return None
    return {
        "verdict": "PASS",
        "disposition": close_class,
        "replacement": replacement,
        "model": "none (deterministic disposition)",
        "runner": "close_disposition",
        "findings": [],
    }


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
    decorated = list(manifest)
    decorated[0] = f"completion-verifier: DISPOSITION {disposition}"
    replacement = result.get("replacement")
    if replacement:
        # Inserted right after the first line so the manifest reads as a sentence, and BEFORE the
        # material/sha steps so their positions stay at the end where readers expect them.
        decorated.insert(1, f"replacement: {replacement}")
    return decorated
