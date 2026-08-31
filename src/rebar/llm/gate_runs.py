"""Durable run handles for the long-running MCP gate ops (bug d80d, Phase 2).

The ``review_plan`` / ``verify_completion`` MCP tools can exceed a client's ~60s
deadline and return ``-32001`` while the server keeps running — leaving the caller
with no completion signal and tempting a duplicate, double-charging retry. Phase 1
de-dups a concurrent retry; Phase 2 removes the timeout from the request path
entirely with an async ``*_start`` + poll surface, mirroring ``run_workflow`` /
``get_workflow_status``.

A ``*_start`` tool reserves a ``job_id``, spawns the gate on a background **daemon
thread**, and returns ``{job_id, ticket_id, gate_type, status:"running"}`` in
milliseconds. This module owns the small **local, git-ignored** index under
``.rebar/gate_runs/<job_id>`` that maps a ``job_id`` back to its run record —
mirroring ``llm/workflow/runs.py``'s ``.rebar/workflow_runs/<run_id>`` pointer — plus
the status resolution the poll read tools use.

Two durability layers back a poll:

* the **local index file** here — the fast handle poll (``running`` / ``passed`` /
  ``failed``), written by the daemon's ``finally`` so a settled run always records a
  terminal status even on failure; and
* the gate's **own signed attestation** on the ticket (the git-durable event log),
  read back by :func:`rebar.llm.plan_review_status` for plan-review and
  :func:`verify_completion_status` for completion — the verdict that survives a full
  process/container restart.

The index is git-ignored (``.rebar/*``), so it is per-checkout transient state, not a
new event type: the AUTHORITATIVE verdict is always the durable attestation.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from rebar import _mcp_inflight as _inflight

logger = logging.getLogger(__name__)

# A run whose index still reads ``running`` but whose daemon is no longer active in
# this process is only treated as crashed (``stale-running``) after this grace period,
# so a status poll that races the daemon's very first index write does not misreport.
_STALE_GRACE_SECONDS: float = 5.0

_PLAN_REVIEW = "plan_review"
_VERIFY_COMPLETION = "verify_completion"


def _repo_root(repo_root: str | None) -> Path:
    if repo_root:
        return Path(repo_root)
    from rebar import config

    return Path(config.repo_root())


def _index_dir(repo_root: str | None) -> Path:
    return _repo_root(repo_root) / ".rebar" / "gate_runs"


def record_gate_run(record: dict[str, Any], *, repo_root: str | None = None) -> None:
    """Persist ``record`` (keyed by its ``job_id``) to the local index, last-writer-wins.

    Written twice per run: once with ``status="running"`` before the daemon starts, then
    overwritten from the daemon's ``finally`` with the terminal status/verdict. The write
    is ATOMIC (temp-in-same-dir + ``os.replace``) so a poll racing the daemon can never
    read a torn index. It is best-effort — a failure to record must never mask the gate's
    own durable attestation, so it is logged, not raised."""
    job_id = record.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return
    from rebar._store.fsutil import atomic_write

    try:
        d = _index_dir(repo_root)
        d.mkdir(parents=True, exist_ok=True)
        atomic_write(d / job_id, json.dumps(record))
    except OSError:
        logger.warning("gate_runs: could not record run %s", job_id, exc_info=True)


def read_gate_run(job_id: str, *, repo_root: str | None = None) -> dict[str, Any] | None:
    """The recorded run dict for ``job_id``, or ``None`` when no index entry exists."""
    try:
        raw = (_index_dir(repo_root) / job_id).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return rec if isinstance(rec, dict) else None


def _durable_verdict(
    gate_type: str, ticket_id: str, repo_root: str | None
) -> dict[str, Any] | None:
    """The gate's own signed attestation currency for ``ticket_id``, or ``None``.

    Reuses the existing read-only, no-LLM attestation readers — never a re-run — so a
    poll after a process restart still resolves the authoritative verdict from the git
    store even when the local index is gone. Returns ``None`` only for a gate_type that has
    NO durable-attestation concept; for a plan-review/completion job it ALWAYS returns a
    dict — an explicit ``{ok: False, verdict: 'unknown', reason}`` if the read raises —
    rather than silently dropping the field on error (the sibling readers never return
    ``None``, so the caller can rely on a durable dict being present)."""
    if gate_type not in (_PLAN_REVIEW, _VERIFY_COMPLETION):
        return None
    try:
        if gate_type == _PLAN_REVIEW:
            import rebar.llm

            return rebar.llm.plan_review_status(ticket_id, repo_root=repo_root)
        return verify_completion_status(ticket_id, repo_root=repo_root)
    except Exception as exc:  # surface an explicit unknown, never drop the field
        logger.warning("gate_runs: durable verdict read failed for %s", ticket_id, exc_info=True)
        return {"ok": False, "verdict": "unknown", "reason": f"durable read failed: {exc}"}


def _resolve_status(rec: dict[str, Any]) -> str:
    """Fold the index record + live registry into a poll status.

    ``running`` while the daemon is alive; the recorded terminal status once it settles;
    ``stale-running`` when the index still reads ``running`` but no daemon owns the job
    (a crashed leader) past the grace window."""
    recorded = str(rec.get("status") or "running")
    job_id = str(rec.get("job_id") or "")
    if recorded != "running":
        return recorded
    if _inflight.is_job_active(job_id):
        return "running"
    started = rec.get("started_at")
    if isinstance(started, (int, float)) and (time.time() - started) < _STALE_GRACE_SECONDS:
        return "running"
    return "stale-running"


def gate_run_status(job_id: str, *, repo_root: str | None = None) -> dict[str, Any]:
    """Resolve a ``*_start`` handle to a poll record (no LLM, no execution).

    Returns ``{job_id, status, ticket_id?, gate_type?, verdict?, error?, durable?}`` where
    ``status`` is ``running`` / ``passed`` / ``failed`` / ``stale-running`` / ``attaching``
    / ``unknown``. ``attaching`` means the job is in flight in the live registry but has no
    index record yet — a follower ``*_start`` shares the leader's ``job_id`` and writes no
    index entry, so a poll in the window before the leader's own ``running`` write lands
    would otherwise read a misleading ``unknown``; keep polling. For a plan-review or
    completion job it also attaches ``durable`` — the gate's own signed-attestation
    currency — so a caller can confirm the run's verdict actually persisted (the
    moving-base-ref / passed-but-unsigned question)."""
    rec = read_gate_run(job_id, repo_root=repo_root)
    if rec is None:
        # No index record. If the live registry still knows this job is in flight (a follower
        # polling before the leader's index write), report 'attaching' rather than a
        # missing-entry 'unknown' — the run genuinely exists (bug d80d).
        if _inflight.is_job_active(job_id):
            return {"job_id": job_id, "status": "attaching"}
        return {"job_id": job_id, "status": "unknown"}
    status = _resolve_status(rec)
    out: dict[str, Any] = {
        "job_id": job_id,
        "status": status,
        "ticket_id": rec.get("ticket_id"),
        "gate_type": rec.get("gate_type"),
    }
    if rec.get("verdict") is not None:
        out["verdict"] = rec.get("verdict")
    if rec.get("error") is not None:
        out["error"] = rec.get("error")
    gate_type = str(rec.get("gate_type") or "")
    ticket_id = rec.get("ticket_id")
    if isinstance(ticket_id, str) and ticket_id:
        durable = _durable_verdict(gate_type, ticket_id, repo_root)
        if durable is not None:
            out["durable"] = durable
    return out


def verify_completion_status(ticket_id: str, *, repo_root: str | None = None) -> dict[str, Any]:
    """Read-only currency query for a completion-verifier attestation (the close-gate analog
    of :func:`rebar.llm.plan_review_status`).

    Verifies the ``completion-verifier`` attestation with the environment key AND folds in
    lifecycle/freshness — NO LLM and NO network, just a local HMAC verify plus the same
    :func:`compute_validity` currency classifier the close gate itself runs — so a caller can
    poll a ``verify_completion`` run's durable outcome without a billable re-run. Returns
    ``{ok, verdict, reason, verified_at_sha, signed_at}`` where ``verdict`` is ``certified``
    only when the attestation is valid RIGHT NOW, else the classifier reason
    (``stale-reopened`` / ``stale-material`` / ``not-closed`` / ``unsigned`` / …); ``ok``
    tracks that same currency. A bare HMAC verify is NOT enough — a stale or reopened
    completion attestation still passes the signature check, so reporting ``certified`` from
    ``sig.get('verified')`` alone would read a superseded verdict as current (bug d80d).
    ``verified_at_sha`` / ``signed_at`` are ``None`` when no readable attestation exists."""
    from rebar import _reads, signing
    from rebar.llm.plan_review.attest import (
        _authoritative_head,
        _authoritative_manifest,
        compute_validity,
    )

    status: dict[str, Any] = {
        "ok": False,
        "verdict": "unsigned",
        "reason": "",
        "verified_at_sha": None,
        "signed_at": None,
    }
    try:
        sig = signing.verify_signature(ticket_id, kind="completion-verifier", repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 — a resolve failure is an unsigned/None answer, not a raise
        status["reason"] = str(exc)
        return status
    if not sig.get("verified"):
        status["reason"] = str(sig.get("reason") or "")
        return status
    # The HMAC verified, but that only proves the attestation was signed — NOT that it still
    # describes the ticket's CURRENT state. Route the verdict through ``compute_validity``
    # (structurally identical to how ``plan_review_status`` defers to ``claim_gate_check``) so
    # a STALE or REOPENED completion attestation reports its lifecycle verdict rather than a
    # false ``certified``. An unreadable state fails closed via the classifier's own checks.
    try:
        state = _reads.show_ticket(ticket_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — unreadable state → compute_validity fails closed
        state = {}
    validity = compute_validity(sig, state, "completion-verifier", repo_root=repo_root)
    status["ok"] = bool(validity.get("valid"))
    status["verdict"] = str(validity.get("verdict") or "unsigned")
    status["reason"] = str(validity.get("reason") or "")
    # Enrichment (report even when now-stale, so a caller sees WHAT was verified and against
    # which SHA): ``verify_signature`` returns the manifest as a LIST — read it via the same
    # authoritative helpers ``plan_review_status`` uses: the pinned verified-at-sha step when
    # present, else the authenticated signed HEAD the verify was bound to.
    manifest = _authoritative_manifest(sig)
    status["verified_at_sha"] = signing.verified_at_sha_from_manifest(
        manifest
    ) or _authoritative_head(sig)
    status["signed_at"] = sig.get("signed_at")
    return status
