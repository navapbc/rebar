"""Fast-fail claimability precheck for the plan-review gate.

``rebar review-plan`` runs a billable multi-pass LLM review to earn a claim
attestation. That is wasted work when the ticket cannot be claimed at all — its
status is terminal/paused (``closed`` / ``idea`` / ``blocked``) so ``claim`` would
reject it (exit 10), or it is ``open`` but still blocked by an unclosed dependency
(``ready_to_work`` is false). This module decides that cheaply (one dep-graph read
+ one reduce; **NO LLM, NO network**) and, when the ticket is not claimable, returns
a well-formed INDETERMINATE ``plan_review_verdict`` that ``_run_plan_review`` returns
verbatim BEFORE assembling context or reaching any LLM pass.

An ``in_progress`` ticket is never fast-failed: it is already claimed and worked in
place, so drift/force execution re-reviews (which keep the attestation fresh) stay
legitimate. Only the gate-reviewed graph types (``task``/``story``/``epic``) are
guarded — ``bug``/``session_log`` short-circuit to an exempt PASS with no LLM cost,
so refusing them would save nothing.

Fail-OPEN throughout: any error computing claimability yields ``None`` (the full
review proceeds and surfaces real errors), so a precheck fault never suppresses a
legitimate review.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Statuses from which `claim` (open -> in_progress) is impossible AND a re-review to
# earn a claim attestation is pointless. `in_progress` is deliberately ABSENT: a
# claimed ticket is worked in place and legitimately re-reviewed (drift/force).
_NOT_CLAIMABLE_STATUSES = frozenset({"closed", "idea", "blocked"})

# The graph types the plan-review gate actually reviews under an LLM. Exempt types
# (bug/session_log) cost no LLM, so there is nothing to fast-fail for them.
_REVIEWED_TYPES = ("task", "story", "epic")


def not_claimable_verdict(ticket_id: str, *, cfg, repo_root=None) -> dict[str, Any] | None:
    """Return an INDETERMINATE ``plan_review_verdict`` when ``ticket_id`` is not
    claimable, else ``None`` (a full review proceeds).

    ``cfg`` supplies the ``runner``/``model`` labels stamped on the verdict (mirroring
    the other non-LLM early-return verdicts in ``_run_plan_review``)."""
    from rebar import config as _config
    from rebar._engine_support.reads import deps_state
    from rebar.graph import reduce_ticket

    tracker = str(_config.tracker_dir(repo_root))
    try:
        graph = deps_state(ticket_id, tracker)
    except Exception:  # noqa: BLE001 — fail-open: unresolved/archived/unreadable -> full path reports it
        return None
    resolved_id = graph.get("ticket_id", ticket_id)
    try:
        state = reduce_ticket(os.path.join(tracker, resolved_id)) or {}
    except Exception:  # noqa: BLE001 — fail-open: an unreducible ticket falls through to the full path
        return None
    ticket_type = str(state.get("ticket_type", ""))
    if ticket_type not in _REVIEWED_TYPES:
        return None
    status = str(state.get("status", ""))
    if status == "in_progress":
        return None  # already claimed/worked; drift/force re-reviews are legitimate

    reason: str | None = None
    detail: dict[str, Any] = {}
    if status in _NOT_CLAIMABLE_STATUSES:
        reason = f'ticket {resolved_id} is not claimable: status is "{status}", not "open"'
        detail = {"status": status}
    elif status == "open" and not graph.get("ready_to_work", True):
        open_blockers = _open_blockers(graph, tracker)
        reason = (
            f"ticket {resolved_id} is not claimable yet: blocked by unclosed "
            f"ticket(s) {', '.join(open_blockers) or '(unknown)'}"
        )
        detail = {"status": status, "blockers": open_blockers}
    if reason is None:
        return None

    logger.info("plan review skipped (ticket not claimable): %s", reason)
    return _build_verdict(resolved_id, ticket_type, reason, detail, cfg=cfg)


def _open_blockers(graph: dict[str, Any], tracker: str) -> list[str]:
    """The subset of ``graph['blockers']`` that is not yet closed/tombstoned — the same
    open-vs-closed rule ``build_dep_graph`` uses to compute ``ready_to_work``."""
    from rebar.graph import reduce_ticket

    open_ids: list[str] = []
    for bid in graph.get("blockers") or []:
        try:
            st = reduce_ticket(os.path.join(tracker, bid)) or {}
        except Exception:  # noqa: BLE001 — an unreducible blocker is reported (conservatively) as still-open
            st = {}
        if str(st.get("status", "")) not in ("closed", "deleted"):
            open_ids.append(bid)
    return open_ids


def _build_verdict(
    ticket_id: str, ticket_type: str, reason: str, detail: dict[str, Any], *, cfg
) -> dict[str, Any]:
    """Assemble the INDETERMINATE not-claimable verdict via the shared early-verdict
    builder."""
    remediation = (
        "Make the ticket claimable first (close/unblock its prerequisite, or reopen it "
        "to `open`), then rerun `rebar review-plan`; no plan-review attestation was signed."
    )
    return indeterminate_verdict(
        ticket_id,
        ticket_type=ticket_type,
        finding={"id": "ticket-not-claimable", "reason": reason, **detail},
        coverage_extra={"not_claimable": {"reason": reason, **detail}},
        signature_reason="not-claimable",
        remediation=remediation,
        cfg=cfg,
    )


def indeterminate_verdict(
    ticket_id: str,
    *,
    ticket_type: str,
    finding: dict[str, Any],
    coverage_extra: dict[str, Any],
    signature_reason: str,
    remediation: str,
    cfg,
) -> dict[str, Any]:
    """Shape-valid INDETERMINATE ``plan_review_verdict`` for the non-LLM early returns
    (not-claimable fast-fail, plan-relation-snapshot error): unsigned, ``coverage.llm_ran``
    false, no sidecar, a single ``indeterminate`` finding, and the standard counts.
    ``coverage_extra`` is merged into ``coverage`` (e.g. a diagnostic record)."""
    verdict: dict[str, Any] = {
        "verdict": "INDETERMINATE",
        "ticket_id": ticket_id,
        "ticket_type": ticket_type,
        "blocking": [],
        "advisory": [],
        "coaching": [],
        "overflow": [],
        "indeterminate": [{**finding, "remediation": remediation}],
        "dropped": [],
        "coverage": {
            "llm_ran": False,
            **coverage_extra,
            "counts": {
                "blocking": 0,
                "advisory_surfaced": 0,
                "advisory_overflow": 0,
                "dropped": 0,
                "indeterminate": 1,
            },
        },
        "signature": {"signed": False, "reason": signature_reason},
        "sidecar_emitted": False,
        "runner": getattr(cfg, "runner", None),
        "model": getattr(cfg, "model", None),
        "material_fingerprint": None,
        "remediation": remediation,
    }
    from rebar.llm import findings

    return findings.validate_structured(verdict, "plan_review_verdict")
