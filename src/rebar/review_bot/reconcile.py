"""Backfill reconciler — recover patchsets a dropped webhook never voted (S4b).

Gerrit's ``events-log`` plugin is a STORE of events, not an auto-replay: if a webhook
delivery is lost (receiver restart, transient network) the patchset is never voted and
— because submit REQUIRES the vote — the change sits unsubmittable forever. This poller
closes that loop. On startup and every ``RECONCILE_INTERVAL_SECONDS`` it reads the
events-log, finds patchsets in the rebar project whose CURRENT revision has no
``LLM-Review`` vote, and re-invokes the SAME ``voter.review_and_vote`` — which shares
the single-flight lock + dedup, so a webhook and a backfill for the same patchset never
double-vote.

The reconciler reuses one ``GerritClient`` + ``DedupStore`` so the run is cheap; the
per-patchset Gerrit-side ``has_llm_review_vote`` check is done inside the voter (the
authoritative skip), but we also pre-filter here to avoid spawning needless reviews.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from rebar.review_bot import voter as _voter
from rebar.review_bot.config import ReceiverConfig
from rebar.review_bot.dedup import DedupStore
from rebar.review_bot.gerrit_client import GerritClient, GerritError

logger = logging.getLogger("rebar.review_bot.reconcile")


def _emit(event: str, **fields: Any) -> None:
    logger.info(json.dumps({"event": event, "timestamp": time.time(), **fields}, default=str))


def _candidate_events(events: list[dict], project: str) -> dict[str, dict]:
    """Reduce the events-log to one ``patchset-created``-shaped event per change,
    keeping the LATEST patchset seen (highest patchSet.number) for the rebar project.

    The events-log mixes many event types; we keep the patchset-bearing ones so the
    voter's ``_extract`` can pull change/revision/ref. Keyed by change id."""
    latest: dict[str, dict] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        change = ev.get("change") or {}
        patchset = ev.get("patchSet") or ev.get("patchset") or {}
        if project and change.get("project") and change.get("project") != project:
            continue
        change_id = change.get("id")
        if not change_id or not patchset.get("revision") or not patchset.get("ref"):
            continue
        try:
            num = int(patchset.get("number") or 0)
        except (TypeError, ValueError):
            num = 0
        prev = latest.get(str(change_id))
        prev_num = 0
        if prev is not None:
            prev_ps = prev.get("patchSet") or prev.get("patchset") or {}
            try:
                prev_num = int(prev_ps.get("number") or 0)
            except (TypeError, ValueError):
                prev_num = 0
        if prev is None or num >= prev_num:
            # Normalize to a patchset-created-shaped event the voter understands.
            latest[str(change_id)] = {
                "type": "patchset-created",
                "change": change,
                "patchSet": patchset,
            }
    return latest


async def reconcile_once(
    *,
    config: ReceiverConfig | None = None,
    gerrit: GerritClient | None = None,
    dedup: DedupStore | None = None,
) -> dict[str, int]:
    """Run one backfill pass. Returns ``{scanned, reviewed}`` counts for observability."""
    cfg = config or ReceiverConfig.from_env()
    gc = gerrit or GerritClient(cfg)
    store = dedup or DedupStore(cfg.dedup_db_path)

    try:
        events = await asyncio.to_thread(gc.list_events)
    except GerritError as exc:
        _emit("reconcile_error", error=str(exc), http_status=getattr(exc, "status", None))
        return {"scanned": 0, "reviewed": 0}

    candidates = _candidate_events(events, cfg.project)
    reviewed = 0
    for change_id, ev in candidates.items():
        patchset = ev.get("patchSet") or {}
        revision = str(patchset.get("revision"))
        # Pre-filter: skip if locally recorded OR already voted on Gerrit (cheap skip
        # before spawning a review). The voter re-checks under the lock authoritatively.
        if store.already_voted(change_id, revision):
            continue
        try:
            if await asyncio.to_thread(gc.has_llm_review_vote, change_id, revision):
                continue
        except GerritError as exc:
            _emit("reconcile_check_error", change_id=change_id, error=str(exc))
            continue
        result = await _voter.review_and_vote(ev, config=cfg, gerrit=gc, dedup=store)
        if result.get("status") == "voted":
            reviewed += 1

    _emit("reconcile_done", scanned=len(candidates), reviewed=reviewed)
    return {"scanned": len(candidates), "reviewed": reviewed}


async def reconcile_loop(
    interval: int | None = None,
    *,
    config: ReceiverConfig | None = None,
    gerrit: GerritClient | None = None,
    dedup: DedupStore | None = None,
) -> None:
    """Run ``reconcile_once`` on startup and then every ``interval`` seconds (default
    ``RECONCILE_INTERVAL_SECONDS``). Runs until cancelled (the app lifespan owns it);
    a per-pass failure is logged and the loop continues."""
    cfg = config or ReceiverConfig.from_env()
    every = interval if interval is not None else cfg.reconcile_interval_seconds
    gc = gerrit or GerritClient(cfg)
    store = dedup or DedupStore(cfg.dedup_db_path)
    while True:
        try:
            await reconcile_once(config=cfg, gerrit=gc, dedup=store)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a pass must never kill the loop
            _emit("reconcile_loop_error", error=str(exc))
        await asyncio.sleep(max(1, every))
