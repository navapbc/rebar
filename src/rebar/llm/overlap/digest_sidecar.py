"""The ``TICKET_DIGEST`` sidecar — persist + read + freshness of a ticket's Cupid digest
(epic only-crave-art, story 2d0f).

A content-hash-keyed per-ticket record, modeled on the ``REVIEW_RESULT`` sidecar
(``plan_review/sidecar.py``): a **reducer-ignored** event (NOT in ``KNOWN_EVENT_TYPES``)
so it never enters compiled state / deps / validate / claim / close hot paths and
compaction preserves it. It IS in the write-path allow-list (so it can be emitted) and in
``_NON_REPLAY_KNOWN_TYPES`` (so ``fsck`` recognises it and does not warn). Freshness is
computed ON READ (fail-closed): a content edit / model change / hash-version bump makes a
stored digest read as stale without any write.

Growth is bounded by ``prune(keep=1)`` on every ``emit`` — exactly the REVIEW_RESULT
retention discipline, nothing more. An archived/deleted ticket's digest simply lingers as a
harmless one-record sidecar; it is never read into a hot path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EVENT_TYPE = "TICKET_DIGEST"

# The sidecar payload schema sentinel — a payload whose ``schema`` != this is skipped by
# the reader, so a future shape bump can never feed a stale record to a consumer.
SCHEMA_SENTINEL = "ticket_digest_v1"

# The normalization/hash version. Bump ONLY if ``_normalize_text``'s hashed field-set ever
# changes — a bump invalidates all cached digests deterministically (freshness flips stale).
DIGEST_HASH_VERSION = 1

# Retention: keep only the single latest digest per ticket (a later emit supersedes it).
RETAIN_PER_TICKET = 1


def _normalize_text(state: dict) -> str:
    """title + description + comment bodies, ``"\\n"``-joined (empties skipped).

    A module-LOCAL normalizer — deliberately NOT an import of the module-private
    ``rebar._engine_support.gates._ticket_text`` (no ``rebar.llm.*`` module imports
    ``_engine_support.gates``). The content hash is this sidecar's own freshness key, so
    it need not be byte-identical to any other module's normalizer; what matters is that
    the SAME function is used at write and at freshness-check time.
    """
    parts: list[str] = []
    if state.get("title"):
        parts.append(str(state["title"]))
    if state.get("description"):
        parts.append(str(state["description"]))
    for c in state.get("comments", []) or []:
        body = (c or {}).get("body", "")
        if body:
            parts.append(str(body))
    return "\n".join(parts)


def content_hash(state: dict) -> str:
    """The freshness content hash for a ticket state: ``sha256(_normalize_text(state))``."""
    return hashlib.sha256(_normalize_text(state).encode("utf-8")).hexdigest()


def build_payload(digest: dict, state: dict, *, model: str) -> dict[str, Any]:
    """The stored sidecar payload: the digest plus its freshness key (content hash + model +
    hash version) under the schema sentinel. Deterministic (no timestamps/uuids)."""
    return {
        "schema": SCHEMA_SENTINEL,
        "digest": digest,
        "content_hash": content_hash(state),
        "model": model,
        "digest_hash_version": DIGEST_HASH_VERSION,
    }


def _tracker(tracker: str | None, repo_root) -> str:
    from rebar import config as _config

    return tracker or str(_config.tracker_dir(repo_root))


def _resolve(rid: str, tracker: str) -> str:
    from rebar._engine_support.resolver import resolve_ticket_id

    return resolve_ticket_id(rid, tracker) or rid


def emit(
    digest: dict,
    ticket_id: str,
    *,
    state: dict | None = None,
    model: str,
    repo_root=None,
) -> bool:
    """Append a ``TICKET_DIGEST`` sidecar for ``ticket_id`` and prune to keep=1.

    ``state`` (the current ticket state) is used to compute the content hash; when omitted
    it is read via the ``rebar.llm`` facade. Best-effort: returns False on any failure (a
    failed emit must never fail the caller — enrichment is advisory)."""
    from rebar._commands._seam import append_event

    try:
        if state is None:
            from rebar import _reads

            state = _reads.show_ticket(ticket_id, repo_root=repo_root)
        payload = build_payload(digest, state, model=model)
        tracker = _tracker(None, repo_root)
        append_event(ticket_id, EVENT_TYPE, payload, Path(tracker), repo_root=repo_root)
    except Exception:  # noqa: BLE001 — best-effort sidecar; broad-but-logged, never fails the caller
        logger.warning("TICKET_DIGEST sidecar emit failed; continuing", exc_info=True)
        return False
    prune(ticket_id, repo_root=repo_root)
    return True


def prune(ticket_id: str, *, keep: int = RETAIN_PER_TICKET, repo_root=None) -> int:
    """Bound growth: keep the most-recent ``keep`` ``TICKET_DIGEST`` events for a ticket and
    remove older ones. Returns the count removed. Best-effort and exception-swallowing (the
    sidecars are reducer-ignored, so removing old ones is safe)."""
    try:
        import subprocess

        tracker = _tracker(None, repo_root)
        rid = _resolve(ticket_id, tracker)
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
        subprocess.run(["git", "-C", tracker, "rm", "-q", *rels], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                tracker,
                "commit",
                "-q",
                "--no-verify",
                "-m",
                f"prune: TICKET_DIGEST sidecar for {rid} (retain {keep})",
            ],
            check=True,
            capture_output=True,
        )
        return len(old)
    except Exception:  # noqa: BLE001 — best-effort retention; broad-but-logged, never raises
        logger.warning("TICKET_DIGEST sidecar prune failed; continuing", exc_info=True)
        return 0


def latest_ticket_digest(
    ticket_id: str, *, tracker: str | None = None, repo_root=None
) -> dict[str, Any] | None:
    """Return the most-recent usable ``TICKET_DIGEST`` payload for ``ticket_id`` (the
    ``build_payload`` dict), or ``None``. Guards on the ``ticket_digest_v1`` sentinel and
    walks newest→oldest, tolerating a corrupt newest file. Never raises."""
    try:
        tracker = _tracker(tracker, repo_root)
        rid = _resolve(ticket_id, tracker)
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
                logger.warning("TICKET_DIGEST sidecar %s unreadable; trying older", fname)
                continue
            payload = event.get("data") if isinstance(event, dict) else None
            if isinstance(payload, dict) and payload.get("schema") == SCHEMA_SENTINEL:
                return payload
        return None
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — best-effort reader; broad-but-logged, never raises
        logger.warning("TICKET_DIGEST sidecar read failed; treating as absent", exc_info=True)
        return None


def _active_model(repo_root) -> str:
    from rebar.llm.config import DEFAULT_MODEL, LLMConfig

    try:
        return LLMConfig.from_env(repo_root=repo_root).model
    except Exception:  # noqa: BLE001 — config resolution fallback
        return DEFAULT_MODEL


def freshness(
    ticket_id: str,
    *,
    state: dict | None = None,
    tracker: str | None = None,
    repo_root=None,
) -> str:
    """Digest freshness for a ticket: ``"present-fresh"`` | ``"present-stale"`` | ``"absent"``.

    Computed on read, FAIL-CLOSED: a stored digest is CURRENT iff ALL of stored
    ``content_hash`` == ``sha256(_normalize_text(current))``, stored ``model`` == the active
    model, and stored ``digest_hash_version`` == the current constant. If the current state
    cannot be read, the digest is treated as NOT current (never falsely fresh). Never raises.
    """
    payload = latest_ticket_digest(ticket_id, tracker=tracker, repo_root=repo_root)
    if payload is None:
        return "absent"
    try:
        if state is None:
            from rebar import _reads

            state = _reads.show_ticket(ticket_id, repo_root=repo_root)
        fresh = (
            payload.get("content_hash") == content_hash(state)
            and payload.get("model") == _active_model(repo_root)
            and payload.get("digest_hash_version") == DIGEST_HASH_VERSION
        )
    except Exception:  # noqa: BLE001 — unreadable current state → fail-closed (not fresh)
        logger.warning("TICKET_DIGEST freshness check failed; treating as stale", exc_info=True)
        return "present-stale"
    return "present-fresh" if fresh else "present-stale"
