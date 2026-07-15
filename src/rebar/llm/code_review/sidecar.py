"""Code-review verdict sidecar (epic b744 / WS4).

Plan-review's ``sidecar.emit`` anchors on ``verdict['ticket_id']`` — but code review reviews a
DIFF, which has no ticket. So this emit takes an EXPLICIT ``target_ticket`` and writes a
``REVIEW_RESULT`` event (the same event TYPE plan-review uses) on it, with a code-review payload.
It is called by ``produce_code_review_verdict`` ONLY when a ``target_ticket`` is supplied (e.g. a
ticket-scoped review, or WS6's Gerrit path); the diff-only path emits no event (the verdict dict
is the artifact). Best-effort: a failure never breaks the gate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

# The retention cap is a SINGLE definition owned by plan_review.sidecar (story fde0); code-review
# imports it rather than defining a second literal, so both prune paths are governed by one
# constant. plan_review.sidecar is stdlib-only at import (no cycle back to this module).
from rebar.llm.plan_review.sidecar import RETAIN_PER_TICKET

logger = logging.getLogger(__name__)

EVENT_TYPE = "REVIEW_RESULT"
SCHEMA = "code_review_result_v2"
# Readers accept every schema version ever written (v2 is a lossless superset of v1: it ADDS
# the `dropped`/`indeterminate` pools + per-finding threshold stamps, without changing the
# SURFACED buckets). Old v1 records must still read, so guard on membership not equality.
ACCEPTED_SCHEMAS = ("code_review_result_v1", "code_review_result_v2")
# The impact-model formula version that produced this sidecar's scores (story
# raptorial-galloping-dragon). Stamped top-level so the calibration replay SEGMENTS old-formula vs
# new-formula findings and never pools across versions. Bump on any impact_code shape change.
IMPACT_MODEL_VERSION = "code-v3"


def change_fingerprint(
    change_id: str, revision: str, changed_files: list[str], diff_text: str
) -> str:
    """A stable diff-scoped join key for a code-review artifact (story limestone).

    The plan-review ``material_fingerprint(ctx: PlanContext)`` is ticket-scoped (ticket_id /
    description / file_impact / children) and a diff has no PlanContext, so this is a small NEW
    analogue with the SAME construction — a sha256 over a sorted-key JSON basis, 16-hex prefix —
    keyed by the CHANGE (gerrit change-id + revision + the sorted changed-file set + a hash of the
    diff text) instead of a ticket. Per-finding identity still reuses
    ``plan_review.sidecar.norm_id`` verbatim (a code-review finding shares the finding/criteria
    shape norm_id reads)."""
    basis = {
        "change_id": change_id or "",
        "revision": revision or "",
        "changed_files": sorted(changed_files or []),
        "diff_sha": hashlib.sha256((diff_text or "").encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(basis, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def _with_norm_ids(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp each finding with its reword-tolerant ``norm_id`` (reused from plan-review)."""
    from rebar.llm.plan_review.sidecar import norm_id

    out = []
    for f in findings or []:
        if isinstance(f, dict):
            out.append({**f, "norm_id": norm_id(f)})
        else:
            out.append(f)
    return out


def build_payload(
    verdict: dict[str, Any],
    *,
    target_ticket: str,
    change_id: str = "",
    revision: str = "",
    change_fp: str = "",
) -> dict[str, Any]:
    """A slim sidecar payload from a code_review_verdict (verdict + counts + coverage + the
    findings/coaching), tagged with its schema + the anchor ticket. When change metadata is supplied
    (the reviewbot artifact path) the payload also carries the ``(change_id, revision)`` key, the
    diff-scoped ``change_fingerprint``, and per-finding ``norm_id``s — the join keys a calibration
    corpus needs.

    ``session_id`` and ``deps`` (story revenued-thickset-dassie) are read straight off the
    ``verdict`` dict, so they flow to EVERY emit path — the produce-path emit AND the Gerrit voter
    emit (which passes the same verdict) — with no per-call-site change. ``session_id`` (nullable)
    is None for Gerrit reviews and set by the local persistence path so local memory is queryable by
    session; ``deps`` is the ``{reviewed-file path: sha256}`` content-hash map the region-gated
    novelty floor (blameless-grindable-noctule) compares against next run."""
    return {
        "schema": SCHEMA,
        "impact_model_version": IMPACT_MODEL_VERSION,
        "verdict": verdict.get("verdict"),
        "ticket_id": target_ticket,
        "change_id": change_id,
        "revision": revision,
        "change_fingerprint": change_fp,
        "session_id": verdict.get("session_id"),
        "deps": verdict.get("deps") or {},
        "runner": verdict.get("runner"),
        "model": verdict.get("model"),
        "coverage": verdict.get("coverage", {}),
        "blocking": _with_norm_ids(verdict.get("blocking", [])),
        "advisory": _with_norm_ids(verdict.get("advisory", [])),
        "dropped": _with_norm_ids(verdict.get("dropped", [])),
        "indeterminate": _with_norm_ids(verdict.get("indeterminate", [])),
        "coaching": verdict.get("coaching", []),
    }


def emit(
    verdict: dict[str, Any],
    *,
    target_ticket: str,
    repo_root=None,
    change_id: str = "",
    revision: str = "",
    change_fp: str = "",
) -> bool:
    """Append a ``REVIEW_RESULT`` sidecar event for ``verdict`` on ``target_ticket``. Idempotency
    per (verdict-identity) is not attempted here (a code review is diff-scoped, not run-keyed);
    callers emit once per produced verdict. When change metadata is supplied (the reviewbot
    artifact path) the payload carries the ``(change_id, revision)`` key + ``change_fingerprint``.
    Returns True on success, False on any failure (best-effort — never raises into the gate)."""
    if not target_ticket:
        return False
    try:
        from rebar import config as _config
        from rebar._commands._seam import append_event

        tracker = _config.tracker_dir(repo_root)
        payload = build_payload(
            verdict,
            target_ticket=target_ticket,
            change_id=change_id,
            revision=revision,
            change_fp=change_fp,
        )
        append_event(target_ticket, EVENT_TYPE, payload, tracker, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — sidecar is best-effort; a failure must not fail the gate
        logger.warning("code-review REVIEW_RESULT sidecar emit failed; continuing", exc_info=True)
        return False
    prune(target_ticket, repo_root=repo_root)  # best-effort retention (bounds code-review growth)
    return True


def prune(ticket_id: str, *, keep: int = RETAIN_PER_TICKET, repo_root=None) -> int:
    """Bound REVIEW_RESULT growth on a code-review target ticket: keep the most-recent ``keep``
    sidecar events (filename timestamp order) and remove older ones. Returns the count removed.
    The code-review analogue of :func:`rebar.llm.plan_review.sidecar.prune`, governed by the SAME
    :data:`RETAIN_PER_TICKET` constant (story fde0 — code-review previously had NO prune, so its
    sidecars grew unbounded). Best-effort and exception-swallowing — a failed prune never fails the
    gate; the sidecars are reducer-ignored, so removing old ones is safe (not state-bearing)."""
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
        delete_events(tracker, rels, f"prune: REVIEW_RESULT sidecar for {rid} (retain {keep})")
        return len(old)
    except Exception:  # noqa: BLE001 — best-effort retention prune; broad-but-logged below, never fails the gate
        logger.warning("code-review REVIEW_RESULT sidecar prune failed; continuing", exc_info=True)
        return 0


# ── reviewed-file content-hash map (deps) — the region-gate state ────────────────────────────────
def _cited_paths_code_review(verdict: dict[str, Any]) -> set[str]:
    """The FILE paths cited by a code-review verdict's SURFACED findings, parsed from each finding's
    ``location`` string (``path`` or ``path:line``) across the ``blocking`` + ``advisory`` buckets.

    This is the code-review analogue of ``plan_review.attest._cited_paths`` — which is NOT reusable
    here because it reads a ``citations`` list with ``kind == "file"``, and code-review findings
    carry no such list (they only have a ``location`` string). Empty / non-path locations are
    ignored; a trailing ``:line[:col]`` is stripped to keep the path component."""
    out: set[str] = set()
    for bucket in ("blocking", "advisory"):
        for f in verdict.get(bucket) or []:
            if not isinstance(f, dict):
                continue
            loc = str(f.get("location") or "").strip()
            if not loc:
                continue
            path = loc.split(":", 1)[0].strip()  # "src/foo.py:42" -> "src/foo.py"
            if path:
                out.add(path)
    return out


def reviewed_file_hashes(paths, *, repo_root=None) -> dict[str, str]:
    """A ``{path: sha256}`` content-hash map over a code review's reviewed files, REUSING the
    private ``plan_review.attest._hash_file`` primitive (raw file-bytes sha256; a missing/unreadable
    path → the ``absent`` sentinel, so a create/delete is detectable). A THIN collector — it does
    NOT reuse ``dependency_hashes`` (that is plan/ticket-coupled: it pulls ticket file_impact +
    plan-verdict citation buckets). The region-gated novelty floor re-hashes these same paths next
    run and compares, so an UNCHANGED file's hash matches (content-addressed, rebase-resilient).

    The base resolves to the WORKING TREE (``_hash_basis`` with no active gate snapshot), which is
    the reviewed basis for both keyspaces: a local ``review-code`` reviews the working tree, and the
    Gerrit voter bot checks out the reviewed revision. Best-effort — never raises."""
    from rebar.llm.plan_review import attest

    try:
        base = attest._hash_basis(repo_root)
        return {p: attest._hash_file(p, base=base) for p in sorted(set(paths or []))}
    except Exception:  # noqa: BLE001 — deps collection is best-effort; never fails the gate
        logger.warning(
            "code-review reviewed_file_hashes failed; returning empty deps", exc_info=True
        )
        return {}


def _ts_prefix(fname: str) -> int:
    """The leading ns-epoch integer of a sidecar filename (0 if unparseable)."""
    head = fname.split("-", 1)[0]
    try:
        return int(head)
    except ValueError:
        return 0


def _latest_payload_with_ts(ticket_id: str, *, repo_root=None) -> tuple[dict[str, Any] | None, int]:
    """The newest usable ``code_review_result_v1`` sidecar payload for ``ticket_id`` + its ns
    timestamp (from the filename prefix), walking newest→oldest and tolerating a malformed/foreign
    newest file. ``(None, -1)`` when nothing usable. Best-effort; never raises."""
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
        for fname in reversed(files):
            try:
                with open(os.path.join(ticket_dir, fname), encoding="utf-8") as fh:
                    event = json.load(fh)
            except (OSError, ValueError):
                logger.warning("code_review sidecar %s unreadable; trying older", fname)
                continue
            payload = event.get("data") if isinstance(event, dict) else None
            if isinstance(payload, dict) and payload.get("schema") in ACCEPTED_SCHEMAS:
                return payload, _ts_prefix(fname)
        return None, -1
    except (FileNotFoundError, NotADirectoryError):
        return None, -1
    except Exception:  # noqa: BLE001 — reader is best-effort; never raises into the floor
        logger.warning("code_review _latest_payload_with_ts failed", exc_info=True)
        return None, -1


def all_review_results(ticket_id: str, *, repo_root=None) -> list[dict[str, Any]]:
    """Return **all** retained code-review ``REVIEW_RESULT`` sidecar payloads on ``ticket_id``,
    newest→oldest, as a list of usable payload dicts (``[]`` when none).

    The full-history analogue of :func:`_latest_payload_with_ts` (story 46f0's audit read
    layer): same ticket-dir resolution and the same :data:`ACCEPTED_SCHEMAS` guard (accepts
    both ``code_review_result_v1`` and ``_v2``), same best-effort posture — it **never
    raises**. NOTE: this reads the events on the GIVEN ticket id directly (the code_review
    artifact ticket), NOT via the session/change title lookup ``latest_code_review_result``
    performs. Unreadable/foreign-schema files are skipped (logged once); a missing ticket dir
    or any error degrades to ``[]``."""
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
        out: list[dict[str, Any]] = []
        for fname in reversed(files):
            try:
                with open(os.path.join(ticket_dir, fname), encoding="utf-8") as fh:
                    event = json.load(fh)
            except (OSError, ValueError):
                logger.warning("code_review sidecar %s unreadable; skipping", fname)
                continue
            payload = event.get("data") if isinstance(event, dict) else None
            if isinstance(payload, dict) and payload.get("schema") in ACCEPTED_SCHEMAS:
                out.append(payload)
        return out
    except (FileNotFoundError, NotADirectoryError):
        return []
    except Exception:  # noqa: BLE001 — best-effort observability reader; broad-but-logged, never raises
        logger.warning(
            "code_review sidecar history read failed; treating as no history", exc_info=True
        )
        return []


def latest_code_review_result(key: str, *, repo_root=None) -> dict[str, Any] | None:
    """Return the most-recent SURFACED code-review findings + ``deps`` map for a TYPED memory key,
    or ``None`` when nothing usable — the reader the region-gated novelty floor consumes.

    ``key`` is typed (disjoint keyspaces, so a prior LOCAL review can never seed a change's FIRST
    Gerrit review):

    - ``session:<id>`` (local) → the artifact whose title == ``code-review: session:{id}`` (exact).
    - ``change:<id>`` (Gerrit) → strip the ``change:`` tag and match artifacts whose title starts
      with ``code-review: {id} @`` (spans revisions of the same change; the change is the memory
      key, the revision only makes each artifact idempotent).

    SURFACED-only (mandatory; bug old-frilly-plankton): ``code_review_result_v1`` stores findings in
    SEPARATE ``blocking``/``advisory``/``coaching`` buckets (no per-finding ``decision``), so
    "surfaced" is the UNION of the ``blocking`` + ``advisory`` buckets — the reader returns ONLY
    those two (never ``coaching`` or any future dropped bucket), so a dropped finding can never
    re-enter the novelty prior set.

    Posture mirrors ``plan_review.sidecar.latest_review_result``: best-effort, NEVER raises,
    schema-guarded to ``code_review_result_v1``, walks newest→oldest per artifact. A reader error
    degrades to ``None`` (⇒ the floor runs un-narrowed = no drops)."""
    try:
        kind, _, ident = str(key or "").partition(":")
        if not ident:
            return None
        if kind == "session":
            wanted = f"code-review: session:{ident}"

            def _match(t: Any) -> bool:
                return str(t.get("title") or "") == wanted
        elif kind == "change":
            prefix = f"code-review: {ident} @"

            def _match(t: Any) -> bool:
                return str(t.get("title") or "").startswith(prefix)
        else:
            return None

        import rebar

        arts = rebar.list_tickets(ticket_type="code_review", repo_root=repo_root) or []
        best_payload: dict[str, Any] | None = None
        best_ts = -1
        for t in arts:
            if not _match(t):
                continue
            aid = str(t.get("ticket_id") or t.get("id") or "")
            if not aid:
                continue
            payload, ts = _latest_payload_with_ts(aid, repo_root=repo_root)
            if payload is not None and ts > best_ts:
                best_payload, best_ts = payload, ts
        if best_payload is None:
            return None
        surfaced = list(best_payload.get("blocking") or []) + list(
            best_payload.get("advisory") or []
        )
        return {
            "findings": surfaced,
            "deps": best_payload.get("deps") or {},
            "session_id": best_payload.get("session_id"),
            "change_id": best_payload.get("change_id"),
        }
    except Exception:  # noqa: BLE001 — reader is best-effort; a failure ⇒ no prior memory (no drops)
        logger.warning(
            "code-review latest_code_review_result failed; treating as no prior memory",
            exc_info=True,
        )
        return None
