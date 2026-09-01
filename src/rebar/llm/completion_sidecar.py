"""The ``COMPLETION_VERDICT`` observability sidecar for completion verdicts.

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

import hashlib
import json
import logging
import os
from collections.abc import Mapping
from typing import Any

from rebar._store.ticket_layout import ticket_dir as layout_ticket_dir
from rebar._store.ticket_layout import ticket_dir_relpath

logger = logging.getLogger(__name__)

EVENT_TYPE = "COMPLETION_VERDICT"
SCHEMA = "completion_verifier_fail_v1"
# The PASS-side sidecar schema (story e7e0): a PASS now leaves a durable record too, carrying
# the lossless positive per-criterion `criteria[]`. Distinct schema tag so the FAIL reader
# (`latest_fail_verdict`, guarded to SCHEMA) and the PASS reader (`latest_pass_record`, guarded
# to SCHEMA_PASS) never confuse the two. The FAIL path/schema/reader are UNCHANGED.
SCHEMA_PASS = "completion_verifier_pass_v1"
# The epic-close bug-screen tally schema (ticket 4b54): one record per screened epic close,
# carrying the per-bug forced-choice verdict + citation and the unevaluated-overflow count
# (candidates beyond the screen ceiling), for audit and live false-negative calibration.
# Distinct schema tag so the FAIL/PASS readers never confuse it with a verdict record.
SCHEMA_SCREEN = "epic_bug_screen_v1"

# Retention bound: COMPLETION_VERDICT is reducer-IGNORED and compaction intentionally
# PRESERVES it (never snapshots/absorbs a non-KNOWN type). This is the default bound for an
# EXPLICIT operator prune — the write path never invokes it (see `prune`).
RETAIN_PER_TICKET = 10

# Write-lock contention (``LockTimeout``, ticket ab54) is a TRANSIENT, retryable failure — a
# concurrent writer held the store lock, not a permanent error. A single re-attempt lets a
# brief contention window clear so the record LANDS instead of being silently dropped. This is
# the total number of append attempts (one retry).
_SIDECAR_WRITE_ATTEMPTS = 2
_PROMPT_ID = "completion-verifier"
_MATERIAL_SCHEMA = "completion_verifier_material_v1"
PINNED_MATERIAL_BASIS = "pinned_completion_inputs"
UNPINNED_MATERIAL_BASIS = "unpinned_completion_inputs"
ERROR_MATERIAL_BASIS = "error_unpinned"


def _short_hash(obj: Mapping[str, Any]) -> str:
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _nonempty_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def verifier_version(repo_root=None) -> dict[str, str]:
    """Return the completion verifier's prompt/formula version stamp.

    Best-effort observability: callers must never change a gate outcome merely because version
    stamping could not resolve a prompt override, so the shape remains present with ``unknown``.
    """
    prompt_hash = "unknown"
    try:
        from rebar.llm.prompting import prompts
        from rebar.llm.prompting.prompt_model import prompt_content_hash

        prompt = prompts.get_prompt(_PROMPT_ID, repo_root=repo_root)
        prompt_text = prompts.canonical_prompt_text(prompt, repo_root=repo_root)
        prompt_hash = prompt_content_hash(prompt_text)
    except Exception:
        logger.warning("completion verifier prompt version unavailable", exc_info=True)
    try:
        from rebar import signing

        formula = signing.gate_code_version()
    except Exception:
        logger.warning("completion verifier formula version unavailable", exc_info=True)
        formula = "unknown"
    return {
        "prompt_id": _PROMPT_ID,
        "prompt_content_sha256": prompt_hash,
        "formula_version": formula,
    }


def _compact_completion_basis(raw: object) -> dict[str, str] | None:
    if not isinstance(raw, Mapping):
        return None
    out = {
        key: raw.get(key)
        for key in ("code_oid", "tickets_oid", "receipt_digest", "run_id")
        if _nonempty_str(raw.get(key))
    }
    return {str(k): str(v) for k, v in out.items()} if "receipt_digest" in out else None


def _prefetch_manifest(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    manifest: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        path = _nonempty_str(item.get("path"))
        mode = _nonempty_str(item.get("mode"))
        if path and mode:
            manifest.append({"path": path, "mode": mode})
    return manifest


def _current_plan_material(ticket_id: str | None, repo_root) -> str | None:
    if not ticket_id:
        return None
    try:
        from rebar.llm.plan_review.attest import current_material_fingerprint

        return current_material_fingerprint(ticket_id, repo_root=repo_root)
    except Exception:
        logger.warning("completion sidecar material fallback unavailable", exc_info=True)
        return None


def _material_record(
    verdict: Mapping[str, Any],
    *,
    material: str | None,
    repo_root=None,
    fallback_basis: str = UNPINNED_MATERIAL_BASIS,
) -> tuple[str | None, str | None]:
    plan_material = _nonempty_str(material) or _nonempty_str(verdict.get("material_fingerprint"))
    if plan_material is None:
        plan_material = _current_plan_material(_nonempty_str(verdict.get("ticket_id")), repo_root)
    if plan_material is None:
        return None, None
    basis = _compact_completion_basis(verdict.get("completion_read_basis"))
    material_basis = PINNED_MATERIAL_BASIS if basis else fallback_basis
    record: dict[str, Any] = {
        "schema": _MATERIAL_SCHEMA,
        "material_basis": material_basis,
        "plan_material_fingerprint": plan_material,
    }
    if basis:
        record["completion_read_basis"] = basis
    manifest = _prefetch_manifest(verdict.get("completion_prefetch_manifest"))
    if manifest:
        record["completion_prefetch_manifest"] = manifest
    return _short_hash(record), material_basis


def error_material(ticket_id: str, *, repo_root=None) -> tuple[str | None, str | None]:
    """Best-effort material identity for completion ERROR sidecars."""
    return _material_record(
        {"ticket_id": ticket_id},
        material=None,
        repo_root=repo_root,
        fallback_basis=ERROR_MATERIAL_BASIS,
    )


def _is_lock_timeout_error(exc: Exception) -> bool:
    """Whether ``exc`` is a write-lock contention (``LockTimeout``) — directly, or wrapped.

    ``_seam.append_event`` converts a store-lock ``LockTimeout`` into a ``CommandError``
    (``returncode`` 1, message ``"flock: could not acquire lock after Ns"``) on the normal
    write path, so a caller retrying on contention must recognise BOTH forms."""
    from rebar._store.lock import LockTimeout

    if isinstance(exc, LockTimeout):
        return True
    return getattr(exc, "returncode", None) == 1 and str(exc).startswith("flock:")


def _append_sidecar_retrying(ticket_id: str, payload: dict[str, Any], tracker, repo_root) -> None:
    """Append a ``COMPLETION_VERDICT`` event, RETRYING once on write-lock contention.

    A ``LockTimeout`` (raw, or wrapped by ``append_event`` into a ``CommandError``) is
    retried up to :data:`_SIDECAR_WRITE_ATTEMPTS` total attempts; any other exception, or a
    contention that persists across every attempt, propagates to the caller (which logs it
    and returns ``False`` — best-effort). This is the ab54 fix: a transient lock timeout no
    longer drops the record on the first hit."""
    from rebar._commands._seam import SecretScreenRefused, append_event

    last_exc: Exception | None = None
    for _ in range(_SIDECAR_WRITE_ATTEMPTS):
        try:
            append_event(ticket_id, EVENT_TYPE, payload, tracker, repo_root=repo_root)
            return
        except SecretScreenRefused:
            # A write-time secret-screen REFUSAL is DETERMINISTIC, not transient contention: a
            # retry would refuse identically. Propagate immediately so the caller can surface it
            # loudly (ticket 4802) rather than burning the retry budget on it.
            raise
        except Exception as exc:
            if not _is_lock_timeout_error(exc):
                raise
            last_exc = exc
    assert last_exc is not None  # loop ran at least once and only continues on a caught exc
    raise last_exc


def emit(verdict: dict[str, Any], *, material: str | None = None, repo_root=None) -> bool:
    """Append a ``COMPLETION_VERDICT`` sidecar event from a completion FAIL verdict.
    Append-ONLY: it never deletes a committed event, so independent clones always
    reconverge by union (store invariant I1) — bounding growth is :func:`prune`, invoked
    explicitly by an operator. Returns True on success, False on any failure (the
    sidecar is observability — a failed persist must NEVER fail the close itself, and the
    FAIL that triggered it still raises regardless). Best-effort.

    A ``LockTimeout`` (transient write-lock contention) is RETRIED once before giving up
    (ticket ab54), so a brief contention window no longer silently loses the record; only a
    contention that persists across every attempt returns ``False``."""
    from rebar import config as _config
    from rebar._commands._seam import SecretScreenRefused, warn_secret_screen_refused

    try:
        tracker = _config.tracker_dir(repo_root)
        payload = build_payload(
            verdict,
            material=material,
            repo_root=repo_root,
            verifier_version=verifier_version(repo_root),
        )
        _append_sidecar_retrying(payload["ticket_id"], payload, tracker, repo_root)
    except SecretScreenRefused:
        warn_secret_screen_refused(str(verdict.get("ticket_id", "?")), EVENT_TYPE)
        return False
    except Exception:
        # Observability floor: the sidecar is best-effort — a failed emit must never fail
        # the close, but the failure itself is a real signal worth a stderr diagnostic.
        logger.warning("COMPLETION_VERDICT sidecar emit failed; continuing", exc_info=True)
        return False
    return True


def prune(ticket_id: str, *, keep: int = RETAIN_PER_TICKET, repo_root=None) -> int:
    """Bound COMPLETION_VERDICT growth: keep the most-recent ``keep`` sidecar events for a
    ticket (filename timestamp order) and remove older ones. Returns the count removed.

    EXPLICIT, operator-invoked: never call this from a write path. Deleting a committed
    UUID event pairs with the appended replacement as a rename in each clone, so two
    clones deleting the same base event conflict instead of reconverging (invariant I1).

    Best-effort and exception-swallowing — a failed prune never fails the close; the
    sidecars are reducer-ignored, so removing old ones is safe (not state-bearing)."""
    try:
        from rebar import config as _config
        from rebar._engine_support.resolver import resolve_ticket_dir_name
        from rebar._store.event_append import delete_events

        tracker = str(_config.tracker_dir(repo_root))
        rid = resolve_ticket_dir_name(ticket_id, tracker)
        ticket_dir = layout_ticket_dir(tracker, rid)
        files = sorted(
            f
            for f in os.listdir(ticket_dir)
            if f.endswith(f"-{EVENT_TYPE}.json") and not f.startswith(".")
        )
        old = files[: max(0, len(files) - keep)]
        if not old:
            return 0
        base_relpath = ticket_dir_relpath(tracker, rid)
        rels = [f"{base_relpath}/{f}" for f in old]
        # Delete through the canonical locked write path (bug malevolent-emigratory-umbrette):
        # a raw git rm + whole-index commit here races normal store writes.
        delete_events(tracker, rels, f"prune: COMPLETION_VERDICT sidecar for {rid} (retain {keep})")
        return len(old)
    except Exception:
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
        from rebar._engine_support.resolver import resolve_ticket_dir_name

        tracker = str(_config.tracker_dir(repo_root))
        rid = resolve_ticket_dir_name(ticket_id, tracker)
        ticket_dir = layout_ticket_dir(tracker, rid)
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
    except Exception:
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
        from rebar._engine_support.resolver import resolve_ticket_dir_name

        tracker = str(_config.tracker_dir(repo_root))
        rid = resolve_ticket_dir_name(ticket_id, tracker)
        ticket_dir = layout_ticket_dir(tracker, rid)
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
    except Exception:
        logger.warning(
            "COMPLETION_VERDICT PASS sidecar read failed; treating as no prior record",
            exc_info=True,
        )
        return None


def emit_screen_tally(
    ticket_id: str, tally: list[dict[str, Any]], *, overflow: int = 0, repo_root=None
) -> bool:
    """Append the epic-close bug-screen tally (ticket 4b54) as a ``COMPLETION_VERDICT``
    sidecar event under :data:`SCHEMA_SCREEN`.

    Best-effort like :func:`emit` — the tally is observability (audit + live false-negative
    calibration); a failed persist must NEVER fail the close. Returns True on success."""
    from rebar import config as _config
    from rebar._commands._seam import append_event

    payload: dict[str, Any] = {
        "schema": SCHEMA_SCREEN,
        "ticket_id": ticket_id,
        "tally": tally,
        "evaluated": len(tally),
        "overflow": overflow,
    }
    try:
        tracker = _config.tracker_dir(repo_root)
        append_event(ticket_id, EVENT_TYPE, payload, tracker, repo_root=repo_root)
    except Exception:
        logger.warning("epic bug screen tally sidecar emit failed; continuing", exc_info=True)
        return False
    return True


def latest_screen_tally(ticket_id: str, *, repo_root=None) -> dict[str, Any] | None:
    """The most-recent :data:`SCHEMA_SCREEN` sidecar payload for ``ticket_id`` (or None).

    Mirrors :func:`latest_pass_record`: newest-first walk, schema-guarded, best-effort —
    never raises."""
    try:
        from rebar import config as _config
        from rebar._engine_support.resolver import resolve_ticket_dir_name

        tracker = str(_config.tracker_dir(repo_root))
        rid = resolve_ticket_dir_name(ticket_id, tracker)
        ticket_dir = layout_ticket_dir(tracker, rid)
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
                continue
            payload = event.get("data") if isinstance(event, dict) else None
            if isinstance(payload, dict) and payload.get("schema") == SCHEMA_SCREEN:
                return payload
        return None
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("epic bug screen tally read failed; treating as absent", exc_info=True)
        return None


def build_payload(
    verdict: dict[str, Any],
    *,
    material: str | None = None,
    repo_root=None,
    verifier_version: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The slim, queryable sidecar payload for a completion verdict.

    The verdict is normalized through the shared :func:`rebar.llm.completion.reconcile_verdict`
    guardrail (idempotent — production verdicts are already reconciled) on a shallow copy, so
    the sidecar always carries the FAIL⇔findings invariant and the remediation guidance that
    reconcile attaches to every FAIL, regardless of the caller. Keeps only the fields worth
    querying offline; runtime-only carriers are dropped to keep the record lean.

    Branches on the (reconciled) verdict: a **PASS** emits the ``SCHEMA_PASS`` record carrying the
    lossless positive ``criteria[]`` (findings empty on PASS); a **FAIL** emits the ``SCHEMA``
    payload (findings/remediation/certifiable) plus additive observability fields."""
    from rebar.llm.completion import reconcile_verdict

    v = dict(verdict)  # shallow copy — reconcile_verdict mutates its argument in place
    reconcile_verdict(v)
    material_fingerprint, material_basis = _material_record(
        v, material=material, repo_root=repo_root
    )
    version = (
        verifier_version
        if verifier_version is not None
        else globals()["verifier_version"](repo_root)
    )
    # Run CONSUMPTION metrics (df94): the gate-run's consumed requests/tool_calls + duration,
    # attached by gate_dispatch._attach_completion_metrics for verdicts produced by an actual
    # LLM run. Carried on BOTH the PASS and FAIL branches so a FAILING gate run's consumption
    # survives to the sidecar (parity with the plan-review REVIEW_RESULT `metrics` block).
    # Absent for a deterministic short-circuit (no LLM ran), so it is only carried when present.
    metrics = v.get("metrics") if isinstance(v.get("metrics"), dict) else None
    if str(v.get("verdict", "")).upper() == "PASS":
        payload = {
            "schema": SCHEMA_PASS,
            "verdict": v.get("verdict"),
            "ticket_id": v.get("ticket_id"),
            "criteria": v.get("criteria", []) or [],
            "findings": [],  # failures-only; a PASS has none
            "runner": v.get("runner"),
            "model": v.get("model"),
            "provider_provenance": v.get("provider_provenance"),
            # The immutable commit the verifier ran against + the model-run id (bug e458): two
            # COMPLETION_VERDICT records can now be compared for input-identity from stored data
            # alone, so a flipped verdict is provably non-determinism on identical input rather
            # than ambiguous input drift. `verified_at_sha` is None in local (unattested) mode.
            "verified_at_sha": v.get("verified_at_sha"),
            "trace_id": v.get("trace_id"),
            "material_fingerprint": material_fingerprint,
            "material_basis": material_basis,
            "verifier_version": version,
            # Whether the close may be CERTIFIED (signed). False iff certification was
            # withheld (an uncertified descendant) — previously dropped on PASS, which made
            # an unsigned certifiable=False close unexplainable from stored data (bug 96d1).
            "certifiable": v.get("certifiable"),
        }
        if metrics is not None:
            payload["metrics"] = dict(metrics)
        # Carry the close gate's bounded auto-resume trail (ticket b5f8) when present, so a
        # PASS reached via resumption stays diagnosable from the durable record alone.
        if v.get("auto_resume_trail"):
            payload["auto_resume_trail"] = list(v["auto_resume_trail"])
        if isinstance(v.get("completion_read_basis"), dict):
            payload["completion_read_basis"] = dict(v["completion_read_basis"])
        prefetch_manifest = _prefetch_manifest(v.get("completion_prefetch_manifest"))
        if prefetch_manifest:
            payload["completion_prefetch_manifest"] = prefetch_manifest
        if v.get("ticket_read_mode"):
            payload["ticket_read_mode"] = v["ticket_read_mode"]
        return payload
    payload = {
        "schema": SCHEMA,
        "verdict": v.get("verdict"),
        "ticket_id": v.get("ticket_id"),
        "findings": v.get("findings", []) or [],
        # Per-criterion records now ride on the FAIL record too (they carry the per-criterion
        # `evidence_sufficient` markers), so an insufficiency FAIL stays diagnosable from the
        # durable record alone.
        "criteria": v.get("criteria", []) or [],
        "remediation": v.get("remediation"),
        "certifiable": v.get("certifiable"),
        "runner": v.get("runner"),
        "model": v.get("model"),
        "provider_provenance": v.get("provider_provenance"),
        # See the PASS branch: verify-sha + trace id for input-identity diagnosis (bug e458).
        "verified_at_sha": v.get("verified_at_sha"),
        "trace_id": v.get("trace_id"),
        "material_fingerprint": material_fingerprint,
        "material_basis": material_basis,
        "verifier_version": version,
    }
    # Carry the verifier-FAULT marker (bug 2a6f) onto the durable record when set, so a run
    # that produced no usable verdict stays queryable AS a fault instead of looking, forever
    # after, like a genuine unmet criterion. Only written when present, so every existing
    # FAIL record keeps its exact prior shape.
    if v.get("verdict_obtainable") is False:
        payload["verdict_obtainable"] = False
    # Carry the insufficient-evidence marker (ticket 1d71) the same way: only written when
    # set, so every existing FAIL record keeps its exact prior shape.
    if v.get("evidence_sufficient") is False:
        payload["evidence_sufficient"] = False
    # Carry the bounded auto-resume trail (ticket b5f8) the same way: only written when
    # present, so every existing FAIL record keeps its exact prior shape.
    if v.get("auto_resume_trail"):
        payload["auto_resume_trail"] = list(v["auto_resume_trail"])
    if metrics is not None:
        payload["metrics"] = dict(metrics)
    if isinstance(v.get("completion_read_basis"), dict):
        payload["completion_read_basis"] = dict(v["completion_read_basis"])
    prefetch_manifest = _prefetch_manifest(v.get("completion_prefetch_manifest"))
    if prefetch_manifest:
        payload["completion_prefetch_manifest"] = prefetch_manifest
    if v.get("ticket_read_mode"):
        payload["ticket_read_mode"] = v["ticket_read_mode"]
    return payload
