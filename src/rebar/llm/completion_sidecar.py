"""The ``COMPLETION_VERDICT`` observability sidecar for completion FAILs (ticket 24ec).

Today only a PASS completion verdict leaves a durable artifact (the signed
``completion-verifier`` attestation). A FAIL blocks the close and then VANISHES — the
findings and remediation guidance are surfaced once on stderr and lost. This sidecar
mirrors the plan-review ``REVIEW_RESULT`` sidecar (:mod:`rebar.llm.plan_review.sidecar`):
every blocked completion FAIL emits a slim, queryable ``COMPLETION_VERDICT`` event to the
ticket store, so completion FAILs are recoverable offline instead of ephemeral.

**reducer-IGNORED** sidecar: ``COMPLETION_VERDICT`` is NOT in ``KNOWN_EVENT_TYPES``, so the
reducer skips it (it never enters compiled state, deps, validate, or the close/claim hot
paths) and compaction PRESERVES it (forward-compat payload, never absorbed into a
SNAPSHOT). It IS in the write-path allow-list (``_store.event_append.EVENT_TYPES``, so it
can be emitted) and in ``_NON_REPLAY_KNOWN_TYPES`` (so ``fsck`` recognises it and does not
warn "newer than me"). This mirrors the REVIEW_RESULT precedent, and follows the
preserved-and-ignored-by-older-clones rollout (upgrade reconcile hosts first).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

EVENT_TYPE = "COMPLETION_VERDICT"
SCHEMA = "completion_verifier_fail_v1"
# The PASS-side sidecar schema (story e7e0): a PASS now leaves a durable record too, carrying
# the lossless positive per-criterion `criteria[]`. Distinct schema tag so the FAIL reader
# (`latest_fail_verdict`, guarded to SCHEMA) and the PASS reader (`latest_pass_record`, guarded
# to SCHEMA_PASS) never confuse the two. The FAIL path/schema/reader are UNCHANGED.
SCHEMA_PASS = "completion_verifier_pass_v1"

# Retention bound: COMPLETION_VERDICT is reducer-IGNORED and compaction intentionally
# PRESERVES it (never snapshots/absorbs a non-KNOWN type), so bound its growth here by
# keeping the most-recent RETAIN_PER_TICKET events per ticket.
RETAIN_PER_TICKET = 10


def emit(verdict: dict[str, Any], *, material: str | None = None, repo_root=None) -> bool:
    """Append a ``COMPLETION_VERDICT`` sidecar event from a completion FAIL verdict, then
    prune to the retention bound. Returns True on success, False on any failure (the
    sidecar is observability — a failed persist must NEVER fail the close itself, and the
    FAIL that triggered it still raises regardless). Best-effort."""
    from rebar import config as _config
    from rebar._commands._seam import append_event

    try:
        tracker = _config.tracker_dir(repo_root)
        payload = build_payload(verdict, material=material)
        append_event(payload["ticket_id"], EVENT_TYPE, payload, tracker, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — best-effort observability sidecar; broad-but-logged below, never fails the close
        # Observability floor: the sidecar is best-effort — a failed emit must never fail
        # the close, but the failure itself is a real signal worth a stderr diagnostic.
        logger.warning("COMPLETION_VERDICT sidecar emit failed; continuing", exc_info=True)
        return False
    prune(str(verdict.get("ticket_id") or ""), repo_root=repo_root)  # best-effort retention
    return True


def prune(ticket_id: str, *, keep: int = RETAIN_PER_TICKET, repo_root=None) -> int:
    """Bound COMPLETION_VERDICT growth: keep the most-recent ``keep`` sidecar events for a
    ticket (filename timestamp order) and remove older ones. Returns the count removed.
    Best-effort and exception-swallowing — a failed prune never fails the close; the
    sidecars are reducer-ignored, so removing old ones is safe (not state-bearing)."""
    try:
        from rebar import config as _config
        from rebar._engine_support.resolver import resolve_ticket_id
        from rebar._store.event_append import delete_events

        tracker = str(_config.tracker_dir(repo_root))
        rid = resolve_ticket_id(ticket_id, tracker) or ticket_id
        ticket_dir = os.path.join(tracker, rid)
        files = sorted(
            f
            for f in os.listdir(ticket_dir)
            if f.endswith(f"-{EVENT_TYPE}.json") and not f.startswith(".")
        )
        old = files[: max(0, len(files) - keep)]
        if not old:
            return 0
        rels = [f"{rid}/{f}" for f in old]
        # Delete through the canonical locked write path (bug malevolent-emigratory-umbrette):
        # a raw git rm + whole-index commit here races normal store writes.
        delete_events(tracker, rels, f"prune: COMPLETION_VERDICT sidecar for {rid} (retain {keep})")
        return len(old)
    except Exception:  # noqa: BLE001 — best-effort retention prune; broad-but-logged below, never fails the close
        logger.warning("COMPLETION_VERDICT sidecar prune failed; continuing", exc_info=True)
        return 0


def latest_fail_verdict(ticket_id: str, *, repo_root=None) -> dict[str, Any] | None:
    """Return the **most-recent** ``COMPLETION_VERDICT`` sidecar payload for ``ticket_id``,
    or ``None`` when none is usable.

    Mirrors :func:`rebar.llm.plan_review.sidecar.latest_review_result`: it walks the
    ticket's sidecar events newest→oldest and returns the first usable payload whose
    ``schema`` == :data:`SCHEMA` (a corrupt/foreign newest file does not blind the caller
    to an older valid one). Observability-only and best-effort — it **never raises**, so a
    missing/garbled record degrades gracefully to ``None``."""
    try:
        from rebar import config as _config
        from rebar._engine_support.resolver import resolve_ticket_id

        tracker = str(_config.tracker_dir(repo_root))
        rid = resolve_ticket_id(ticket_id, tracker) or ticket_id
        ticket_dir = os.path.join(tracker, rid)
        files = sorted(
            f
            for f in os.listdir(ticket_dir)
            if f.endswith(f"-{EVENT_TYPE}.json") and not f.startswith(".")
        )
        # Filenames are timestamp-prefixed (fixed-width ns epoch), so reverse order is
        # newest-first. Return the first USABLE v1 payload, tolerating a corrupt newest.
        for fname in reversed(files):
            try:
                with open(os.path.join(ticket_dir, fname), encoding="utf-8") as fh:
                    event = json.load(fh)
            except (OSError, ValueError):
                logger.warning("COMPLETION_VERDICT sidecar %s unreadable; trying older", fname)
                continue
            payload = event.get("data") if isinstance(event, dict) else None
            if isinstance(payload, dict) and payload.get("schema") == SCHEMA:
                return payload
        return None
    except FileNotFoundError:
        return None  # ticket dir absent → no prior FAIL record
    except Exception:  # noqa: BLE001 — best-effort observability reader; broad-but-logged, never raises
        logger.warning(
            "COMPLETION_VERDICT sidecar read failed; treating as no prior record", exc_info=True
        )
        return None


def latest_pass_record(ticket_id: str, *, repo_root=None) -> dict[str, Any] | None:
    """Return the **most-recent** PASS ``COMPLETION_VERDICT`` sidecar payload for ``ticket_id``,
    or ``None`` when none is usable.

    Mirrors :func:`latest_fail_verdict` exactly, but schema-guarded to :data:`SCHEMA_PASS` (the
    lossless PASS record carrying ``criteria[]``): it walks the ticket's sidecar events
    newest→oldest and returns the first usable payload whose ``schema`` == :data:`SCHEMA_PASS`
    (a corrupt/foreign newest file does not blind the caller to an older valid one).
    Observability-only and best-effort — it **never raises**, so a missing/garbled record
    degrades gracefully to ``None``."""
    try:
        from rebar import config as _config
        from rebar._engine_support.resolver import resolve_ticket_id

        tracker = str(_config.tracker_dir(repo_root))
        rid = resolve_ticket_id(ticket_id, tracker) or ticket_id
        ticket_dir = os.path.join(tracker, rid)
        files = sorted(
            f
            for f in os.listdir(ticket_dir)
            if f.endswith(f"-{EVENT_TYPE}.json") and not f.startswith(".")
        )
        # Filenames are timestamp-prefixed (fixed-width ns epoch), so reverse order is
        # newest-first. Return the first USABLE PASS payload, tolerating a corrupt newest.
        for fname in reversed(files):
            try:
                with open(os.path.join(ticket_dir, fname), encoding="utf-8") as fh:
                    event = json.load(fh)
            except (OSError, ValueError):
                logger.warning("COMPLETION_VERDICT sidecar %s unreadable; trying older", fname)
                continue
            payload = event.get("data") if isinstance(event, dict) else None
            if isinstance(payload, dict) and payload.get("schema") == SCHEMA_PASS:
                return payload
        return None
    except FileNotFoundError:
        return None  # ticket dir absent → no prior PASS record
    except Exception:  # noqa: BLE001 — best-effort observability reader; broad-but-logged, never raises
        logger.warning(
            "COMPLETION_VERDICT PASS sidecar read failed; treating as no prior record",
            exc_info=True,
        )
        return None


def build_payload(verdict: dict[str, Any], *, material: str | None = None) -> dict[str, Any]:
    """The slim, queryable sidecar payload for a completion verdict.

    The verdict is normalized through the shared :func:`rebar.llm.completion.reconcile_verdict`
    guardrail (idempotent — production verdicts are already reconciled) on a shallow copy, so
    the sidecar always carries the FAIL⇔findings invariant and the remediation guidance that
    reconcile attaches to every FAIL, regardless of the caller. Keeps only the fields worth
    querying offline; runtime-only carriers are dropped to keep the record lean.

    Branches on the (reconciled) verdict: a **PASS** emits the ``SCHEMA_PASS`` record carrying the
    lossless positive ``criteria[]`` (findings empty on PASS); a **FAIL** keeps the EXACT prior
    ``SCHEMA`` payload (findings/remediation/certifiable) unchanged."""
    from rebar.llm.completion import reconcile_verdict

    v = dict(verdict)  # shallow copy — reconcile_verdict mutates its argument in place
    reconcile_verdict(v)
    if str(v.get("verdict", "")).upper() == "PASS":
        return {
            "schema": SCHEMA_PASS,
            "verdict": v.get("verdict"),
            "ticket_id": v.get("ticket_id"),
            "criteria": v.get("criteria", []) or [],
            "findings": [],  # failures-only; a PASS has none
            "runner": v.get("runner"),
            "model": v.get("model"),
            "material_fingerprint": material,
        }
    return {
        "schema": SCHEMA,
        "verdict": v.get("verdict"),
        "ticket_id": v.get("ticket_id"),
        "findings": v.get("findings", []) or [],
        "remediation": v.get("remediation"),
        "certifiable": v.get("certifiable"),
        "runner": v.get("runner"),
        "model": v.get("model"),
        "material_fingerprint": material,
    }
