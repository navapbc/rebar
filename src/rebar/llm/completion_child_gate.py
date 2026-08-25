"""Deterministic child-closure gate of the completion verifier (no LLM).

The "epic-level verdict trust" rule, checked as a graph + signature invariant rather than an
LLM judgment: a parent's completeness over its DIRECT children is proven deterministically —
an unclosed child blocks the close outright, and a closed-but-uncertified child withholds
certification. :func:`child_closure_findings` computes the invariant;
:func:`build_child_closure_evidence` surfaces the same proof as a compact evidence block for
the verifier's fenced context (ticket 6ec8), so an "every child is closed" criterion resolves
without a tool call.

Consumed through the stable :mod:`rebar.llm.completion` seam by the workflow gate ops
(``rebar.llm.workflow.gate_ops``); the FAIL-verdict construction that pairs with the blocking
findings lives in :mod:`rebar.llm.completion_reconcile` (``deterministic_child_failure``).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _pinned_child_states(ticket_id: str, view) -> list[dict]:
    """Read only the child fields that can affect closure certification."""
    fields = ("title", "status", "archived", "last_reopened_at", "attestations")
    states = [
        {
            "ticket_id": child_id,
            **{field: view.field_value(child_id, field) for field in fields},
        }
        for child_id in view.direct_child_ids(ticket_id)
    ]
    return [state for state in states if not state.get("archived")]


def child_closure_findings(ticket_id: str, repo_root) -> tuple[list[dict], list[dict]]:
    """Deterministic child-closure / certification gate — the "epic-level verdict trust" rule.

    Returns ``(blocking, uncertified)`` for a parent's **direct** children (childless tickets yield
    ``([], [])`` — a natural no-op for most tasks/bugs). Checked deterministically (a graph +
    signature invariant, not an LLM judgment): we DO NOT recurse into grandchildren (each child
    owns its own subtree), and we DO NOT re-verify a child's own completion criteria — a child's
    **certified signature IS** the trusted attestation that its criteria were validated at close.

    * **blocking** — a direct child that is NOT closed. The parent is INCOMPLETE (delegated work
      unfinished): the close gate fails fast WITHOUT an LLM call and closure is BLOCKED.
    * **uncertified** — a direct child that is closed but WITHOUT a certified/valid closure (a
      force-closed / reopened / drift-stale child). Its work is done, but its subtree is
      unattested: the parent may CLOSE (subject to its OWN criteria) but cannot be CERTIFIED —
      certification propagates, so an unattested descendant WITHHOLDS the parent's signature.

    **Read-error path (fail-safe on certification).** If enumerating the children itself fails
    (a transient store read error), we can no longer prove the subtree is attested, so we
    WITHHOLD certification rather than forge it: we return ``([], [<marker>])`` — an EMPTY
    ``blocking`` (the parent may still close on its OWN criteria; a read glitch shouldn't block
    a legitimate close) but a NON-EMPTY ``uncertified`` (so ``certifiable`` is ``False`` and the
    parent closes UNSIGNED). Returning ``([], [])`` here (the old behaviour) would have LAUNDERED
    certification — a read failure would have signed the parent as if it were childless. This
    mirrors ``plan_review.attest._attested_delivered``, which fails closed on the same error."""
    from rebar import _reads
    from rebar._engine_support.reads import current_ticket_view

    view = current_ticket_view()
    try:
        children = (
            _pinned_child_states(ticket_id, view)
            if view is not None
            else _reads.list_tickets(parent=ticket_id, repo_root=repo_root)
        )
    except Exception as exc:
        from rebar._engine_support.reads import reraise_pinned_read_failure

        reraise_pinned_read_failure(exc)
        logger.warning(
            "child-closure enumeration failed for %s; withholding certification "
            "(the parent may still close on its own criteria, but UNSIGNED) rather than "
            "forging it from an unread (assumed-empty) child set",
            ticket_id,
            exc_info=True,
        )
        return [], [
            {
                "criterion": f"direct children of {ticket_id} could not be certified",
                "severity": "high",
                "dimension": "completion",
                "detail": (
                    f"could not enumerate the direct children of {ticket_id} to verify their "
                    f"certified closure ({exc}); WITHHOLDING certification — the parent may still "
                    "close on its OWN criteria but is NOT signed, rather than forging "
                    "certification from an unread (assumed-empty) child set. Re-close once the "
                    "store read succeeds to certify."
                ),
                "citations": [
                    {
                        "kind": "source",
                        "description": f"list_tickets(parent={ticket_id}) read error: {exc}",
                    }
                ],
            }
        ]
    from rebar.llm.plan_review.relation_snapshot import material_child_index

    # bug a3f5-5c19: one lazily-built child-index snapshot shared across every child's
    # compute_validity (whose completion branch fingerprints the child's material —
    # up to 5 full-store scans per stale child) instead of a scan per child.
    # A lazy pinned view cannot answer that context's intentionally-unbounded global scan;
    # its parent-bounded reads are already demand-cached and receipt-tracked, so use them
    # directly and keep the stable-view contract fail-closed (never fall through to live).
    if view is not None:
        return _classify_children(children, repo_root)
    with material_child_index(repo_root=repo_root):
        return _classify_children(children, repo_root)


def _classify_children(children: list[dict], repo_root) -> tuple[list[dict], list[dict]]:
    """The per-child closure/certification classification loop of
    :func:`child_closure_findings`, extracted so the shared ``material_child_index``
    snapshot wraps exactly the loop that fingerprints."""
    import rebar  # verify_signature (not a rebar._reads read) is sourced from the facade

    blocking: list[dict] = []
    uncertified: list[dict] = []
    for c in children:
        cid = c.get("ticket_id")
        if cid is None:
            continue
        title = (c.get("title") or "")[:50]
        status = c.get("status")
        if status != "closed":
            blocking.append(
                {
                    "criterion": f"direct child {cid} is closed",
                    "severity": "high",
                    "dimension": "completion",
                    "detail": f"child {cid} ('{title}') is '{status}', not closed.",
                    "citations": [
                        {"kind": "source", "description": f"ticket {cid} status={status}"}
                    ],
                }
            )
            continue
        # Verify the child's COMPLETION-VERIFIER attestation specifically (epic
        # dark-acme-lumen) — not the most-recent signature of any kind — then run
        # compute_validity so a reopened/materially-edited closure no longer counts as a
        # validated closure (validity-on-read; records are never mutated).
        try:
            from rebar._engine_support.reads import current_ticket_view

            view = current_ticket_view()
            if view is None:
                sig: dict[str, object] = dict(
                    rebar.verify_signature(cid, kind="completion-verifier", repo_root=repo_root)
                )
            else:
                from rebar import signing

                attestations = c.get("attestations") or {}
                record = (
                    attestations.get("completion-verifier")
                    if isinstance(attestations, dict)
                    else None
                )
                sig = dict(
                    signing.verify_attestation_record(
                        record if isinstance(record, dict) else None,
                        cid,
                        kind="completion-verifier",
                        repo_root=repo_root,
                    )
                )
                sig["ticket_id"] = cid
                sig["kind"] = "completion-verifier"
            if sig.get("verdict") == "certified":
                from rebar.llm.plan_review.attest import compute_validity

                v = compute_validity(sig, c, "completion-verifier", repo_root=repo_root)
                valid, detail = v.get("valid", False), v.get("reason", "")
            else:
                # Carry the REASON, not just the verdict: the verdict alone ("unsigned")
                # tells a reader nothing about what to do next (bug 94a3).
                valid, detail = False, f"signature: {sig.get('verdict')} — {sig.get('reason', '')}"
        except Exception as exc:  # noqa: BLE001 — live signature faults remain in-band
            from rebar._engine_support.reads import reraise_pinned_read_failure

            reraise_pinned_read_failure(exc)
            valid, detail = False, f"error: {exc}"
        if not valid:
            uncertified.append(
                {
                    "criterion": f"direct child {cid} has a certified closure",
                    "severity": "high",
                    "dimension": "completion",
                    "detail": (
                        f"child {cid} ('{title}') is closed but its completion closure is not "
                        f"certified/valid ({detail}) — its subtree is unattested, so the parent "
                        "closes WITHOUT certification. Re-close the child through the gate to "
                        "certify (and re-close the parent) if a signed closure is required."
                    ),
                    "citations": [
                        {"kind": "source", "description": f"completion-verifier({cid}): {detail}"}
                    ],
                }
            )
    return blocking, uncertified


def _uncertified_child_ids(uncertified: list[dict]) -> list[str]:
    """The child ids carried in ``uncertified`` findings from :func:`child_closure_findings`.

    Each per-child finding's ``criterion`` is ``f"direct child {cid} has a certified closure"``;
    the read-error marker (``"direct children of … could not be certified"``) carries no id and is
    skipped — but that path also makes :func:`build_child_closure_evidence` return ``""`` (its own
    enumeration fails identically), so no id is ever needed from it."""
    ids: list[str] = []
    for f in uncertified:
        parts = str(f.get("criterion", "")).split()
        if len(parts) >= 3 and parts[0] == "direct" and parts[1] == "child":
            ids.append(parts[2])
    return ids


def build_child_closure_evidence(ticket_id: str, repo_root, uncertified: list[dict]) -> str:
    """A compact, deterministic child-closure evidence block for the completion verifier (6ec8).

    :func:`child_closure_findings` already proves, deterministically, whether every DIRECT child of
    ``ticket_id`` is closed AND carries a certified completion-verifier signature. That proof is
    used for the gate short-circuit but never reaches the verifier's prompt, so an epic AC like
    "every child story is closed" is judged with ZERO evidence. This surfaces the SAME result as a
    few lines of evidence (counts + ids of any closed-but-uncertified children), so such a criterion
    resolves WITHOUT a tool call.

    Reuses the same child enumeration as :func:`child_closure_findings` so the counts stay
    consistent, and derives the uncertified ids from the passed-in ``uncertified`` (never
    re-derived). Returns
    ``""`` when the ticket has no direct children (a childless ticket gets no block — no noise) or
    when enumeration fails. Callers inject the string into the fenced (untrusted) context; the
    caveat governing how the verifier TREATS it lives in the trusted verifier prompt.

    Note: on the caller's LLM path ``blocking`` is necessarily empty (a non-empty ``blocking``
    short-circuited earlier), so only closure+certification is reported here."""
    from rebar import _reads
    from rebar._engine_support.reads import current_ticket_view

    try:
        view = current_ticket_view()
        children = (
            _pinned_child_states(ticket_id, view)
            if view is not None
            else _reads.list_tickets(parent=ticket_id, repo_root=repo_root)
        )
    except Exception as exc:  # noqa: BLE001 — live read failure keeps the legacy no-block evidence path
        from rebar._engine_support.reads import reraise_pinned_read_failure

        reraise_pinned_read_failure(exc)
        return ""
    total = len(children)
    if total == 0:
        return ""
    unc_ids = _uncertified_child_ids(uncertified)
    lines = [
        "## Deterministic child-closure evidence (computed by the close gate, not the LLM)",
        f"This ticket has {total} direct child ticket(s).",
    ]
    if not unc_ids:
        lines.append(
            f"All {total} direct child ticket(s) are CLOSED and carry a certified "
            "completion-verifier signature (deterministically proven)."
        )
    else:
        lines.append(
            f"{total - len(unc_ids)} of {total} direct child ticket(s) are closed AND certified; "
            f"the following {len(unc_ids)} are closed but NOT certified: {', '.join(unc_ids)}."
        )
    lines.append(
        "NOTE: whether each child's change is 'Verified +1' on Gerrit is NOT observable from "
        "repository tools; only closure and certification are proven above."
    )
    return "\n".join(lines)
