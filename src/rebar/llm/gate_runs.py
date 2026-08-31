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
    store even when the local index is gone."""
    try:
        if gate_type == _PLAN_REVIEW:
            import rebar.llm

            return rebar.llm.plan_review_status(ticket_id, repo_root=repo_root)
        if gate_type == _VERIFY_COMPLETION:
            return verify_completion_status(ticket_id, repo_root=repo_root)
    except Exception:
        logger.warning("gate_runs: durable verdict read failed for %s", ticket_id, exc_info=True)
    return None


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
    ``status`` is ``running`` / ``passed`` / ``failed`` / ``stale-running`` / ``unknown``.
    For a plan-review or completion job it also attaches ``durable`` — the gate's own
    signed-attestation currency — so a caller can confirm the run's verdict actually
    persisted (the moving-base-ref / passed-but-unsigned question)."""
    rec = read_gate_run(job_id, repo_root=repo_root)
    if rec is None:
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

    Verifies the ``completion-verifier`` attestation with the environment key — NO LLM and NO
    network, just a local HMAC verify — so a caller can poll a ``verify_completion`` run's
    durable outcome without a billable re-run. Returns ``{ok, verdict, reason,
    verified_at_sha, signed_at}`` where ``verdict`` is ``certified`` when a valid attestation
    exists, else ``unsigned``; ``verified_at_sha`` / ``signed_at`` are ``None`` when none
    does."""
    from rebar import signing
    from rebar.llm.plan_review.attest import _authoritative_head, _authoritative_manifest

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
    if sig.get("verified"):
        status["ok"] = True
        status["verdict"] = "certified"
        status["signed_at"] = sig.get("signed_at")
        # ``verify_signature`` returns the manifest as a LIST (the signed DSSE steps for an
        # op-cert, or the HMAC-covered legacy manifest) — NOT a dict — so read it via the same
        # authoritative helpers ``plan_review_status`` uses: the pinned verified-at-sha step
        # when present, else the authenticated signed HEAD the verify was bound to.
        manifest = _authoritative_manifest(sig)
        status["verified_at_sha"] = signing.verified_at_sha_from_manifest(
            manifest
        ) or _authoritative_head(sig)
    else:
        status["reason"] = str(sig.get("reason") or "")
    return status
