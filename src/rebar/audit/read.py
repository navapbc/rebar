"""The audit read layer's aggregator (story 46f0).

:func:`audit_trail` composes the existing best-effort sidecar readers into ONE documented
read surface for a ticket: its full retained plan-review history, its completion attestation
+ sidecar record, and the code reviews associated with it (resolved via inbound ``relates_to``
links from ``code_review`` artifact tickets, each with its own retained sidecar history).

Every reader it composes is observability-only and best-effort, so this aggregator inherits
that posture: an individual reader failure degrades to ``[]`` / ``None`` and NEVER raises. The
returned shape is the ``AuditTrail`` TypedDict below (a documented, stable contract):

    AuditTrail = {
        "ticket":       dict,                 # rebar.show_ticket(ticket_id)
        "plan_reviews": list[dict],           # full retained plan-review history, newest-first
        "completion":   CompletionRecord|None,# None ONLY when BOTH attestation AND sidecar absent
        "code_reviews": list[dict],           # each {ticket_id, sidecars: list[dict]}
    }
    CompletionRecord = {"attestation": dict|None, "sidecar": dict|None}
"""

from __future__ import annotations

import logging
from typing import TypedDict

logger = logging.getLogger(__name__)


class CompletionRecord(TypedDict):
    """The completion-verifier evidence for a ticket: its signed attestation (``None`` if
    unsigned) and the newest completion sidecar record — a PASS or FAIL (``None`` if none)."""

    attestation: dict | None
    sidecar: dict | None


class AuditTrail(TypedDict):
    """The full audit read surface for one ticket (see module docstring)."""

    ticket: dict
    plan_reviews: list[dict]
    completion: CompletionRecord | None
    code_reviews: list[dict]


def _completion_attestation(ticket_id: str, *, repo_root=None) -> dict | None:
    """The ticket's completion-verifier attestation as a plain dict, or ``None`` when
    unsigned / on any error. Read via :func:`rebar.verify_signature` (kind-scoped to
    ``completion-verifier``); a ``verdict == "unsigned"`` result (no signature record on the
    ticket) is reported as ``None``. Best-effort — never raises."""
    try:
        import dataclasses

        import rebar

        vs = rebar.verify_signature(ticket_id, kind="completion-verifier", repo_root=repo_root)
        if vs is None:
            return None
        rec = dataclasses.asdict(vs) if dataclasses.is_dataclass(vs) else dict(vs)
        # "unsigned" == the ticket carries no completion-verifier signature record at all.
        if str(rec.get("verdict") or "") == "unsigned":
            return None
        return rec
    except Exception:  # noqa: BLE001 — best-effort attestation read; never fails the aggregate
        logger.warning("completion attestation read failed; treating as unsigned", exc_info=True)
        return None


def _completion_sidecar_record(ticket_id: str, *, repo_root=None) -> dict | None:
    """The newest completion sidecar record for ``ticket_id``, or ``None``. A PASS record
    (``latest_pass_record``) wins over a FAIL record (``latest_fail_verdict``) when both
    exist — the documented tie-break for this surface. Best-effort — never raises."""
    try:
        from rebar.llm import completion_sidecar

        rec = completion_sidecar.latest_pass_record(ticket_id, repo_root=repo_root)
        if rec is not None:
            return rec
        return completion_sidecar.latest_fail_verdict(ticket_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — best-effort sidecar read; never fails the aggregate
        logger.warning("completion sidecar read failed; treating as none", exc_info=True)
        return None


def _resolve(ticket_id: str, tracker: str) -> str:
    """Resolve an id/alias/short-prefix to its canonical ticket id (best-effort: falls back
    to the input on any failure, so a mismatch simply fails the equality check)."""
    try:
        from rebar._engine_support.resolver import resolve_ticket_id

        return resolve_ticket_id(ticket_id, tracker) or ticket_id
    except Exception:  # noqa: BLE001 — resolution is best-effort; fall back to the raw id
        return ticket_id


def _related_code_reviews(ticket_id: str, *, repo_root=None) -> list[dict]:
    """The code reviews associated with ``ticket_id``: ``code_review``-type tickets that link
    ``relates_to`` this ticket. Each match is returned as ``{"ticket_id": <cr id>, "sidecars":
    [...]}`` carrying that artifact's full retained code-review sidecar history (newest→oldest).

    Candidates are enumerated via ``list_tickets(ticket_type="code_review")`` and selected when
    any of their ``relates_to`` dep edges resolves to this ticket (ids compared canonically, so a
    full/short/alias target still matches). Best-effort — returns ``[]`` on any error."""
    try:
        import rebar
        from rebar import config as _config
        from rebar.llm.code_review import sidecar as code_sidecar

        tracker = str(_config.tracker_dir(repo_root))
        want = _resolve(ticket_id, tracker)

        candidates = rebar.list_tickets(ticket_type="code_review", repo_root=repo_root) or []
        out: list[dict] = []
        for cand in candidates:
            cid = str(cand.get("ticket_id") or cand.get("id") or "")
            if not cid:
                continue
            deps = cand.get("deps")
            if not isinstance(deps, list):
                # The lean list shape omits deps; fetch the full ticket to read its links.
                try:
                    deps = rebar.show_ticket(cid, repo_root=repo_root).get("deps") or []
                except Exception:  # noqa: BLE001 — best-effort per-candidate; skip on failure
                    continue
            matched = any(
                isinstance(d, dict)
                and d.get("relation") == "relates_to"
                and _resolve(str(d.get("target_id") or ""), tracker) == want
                for d in deps
            )
            if matched:
                out.append(
                    {
                        "ticket_id": cid,
                        "sidecars": code_sidecar.all_review_results(cid, repo_root=repo_root),
                    }
                )
        return out
    except Exception:  # noqa: BLE001 — best-effort resolution; never fails the aggregate
        logger.warning("related code-review resolution failed; returning none", exc_info=True)
        return []


def audit_trail(ticket_id: str, *, repo_root=None) -> dict:
    """Aggregate a ticket's FULL retained review history + associated code reviews into one
    ``AuditTrail`` dict (see module docstring for the exact shape).

    Best-effort throughout: each composed reader degrades to ``[]`` / ``None`` on failure and
    this function never raises. ``completion`` is ``None`` ONLY when BOTH the attestation and
    the completion sidecar record are absent; otherwise it is a ``CompletionRecord`` carrying
    whichever of the two is present (either field may still be ``None``)."""
    from rebar.llm.plan_review import sidecar as plan_sidecar

    ticket = rebar_show(ticket_id, repo_root=repo_root)
    plan_reviews = plan_sidecar.all_review_results(ticket_id, repo_root=repo_root)

    attestation = _completion_attestation(ticket_id, repo_root=repo_root)
    sidecar = _completion_sidecar_record(ticket_id, repo_root=repo_root)
    completion: CompletionRecord | None
    if attestation is None and sidecar is None:
        completion = None
    else:
        completion = {"attestation": attestation, "sidecar": sidecar}

    code_reviews = _related_code_reviews(ticket_id, repo_root=repo_root)

    trail: AuditTrail = {
        "ticket": ticket,
        "plan_reviews": plan_reviews,
        "completion": completion,
        "code_reviews": code_reviews,
    }
    return dict(trail)


def rebar_show(ticket_id: str, *, repo_root=None) -> dict:
    """``rebar.show_ticket(ticket_id)`` with a lazy import (best-effort — an error yields a
    minimal ``{"ticket_id": ...}`` stub rather than raising, keeping the aggregate resilient)."""
    try:
        import rebar

        # show_ticket returns a TicketState (a TypedDict); dict(...) normalizes to the
        # plain-dict the AuditTrail contract declares for ``ticket``.
        return dict(rebar.show_ticket(ticket_id, repo_root=repo_root))
    except Exception:  # noqa: BLE001 — best-effort; a show failure must not sink the whole trail
        logger.warning("show_ticket failed in audit_trail; returning stub", exc_info=True)
        return {"ticket_id": ticket_id}
