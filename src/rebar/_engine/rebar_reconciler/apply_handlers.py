#!/usr/bin/env python3
"""Per-action mutation handlers for the legacy outbound batch path.

``applier._apply_batch`` is the *sequencer*: HEAD-drift recheck loop → dispatch →
record → manifest-write tail. This module owns the *per-action orchestration*
that wraps the ``batch_dispatch`` Jira-call primitives, one handler per
``MutationAction``:

    - ``handle_create`` — REST-budget counting + swallowed-comment surfacing
      around ``create_one``.
    - ``handle_update`` — the 404 / assignee-unresolved per-mutation soft-fails,
      the sub-op (labels/comments/links) telemetry, the bug-3f04 silent-no-op
      canary, and set-valued-field provenance, around ``update_one``.
    - ``handle_delete`` — ``delete_one`` (already-gone tolerance lives in the
      primitive).
    - ``handle_unknown`` — the legacy unrecognised-action error outcome.

Each handler takes the per-pass :class:`BatchApplyContext` plus a single mutation
dict and returns a :class:`HandlerResult` the sequencer appends to the manifest.

Transport is *injected*: the sequencer resolves the AcliClient transport through
``applier._load_acli`` (the seam the tests patch, now returning the configured
backend's transport directly — S4) and hands it to the handlers on the context;
the handlers never resolve transport themselves. This
module imports only *downward* (batch_dispatch / pass_io); it never imports
``applier``, so the sequencer can import the handlers back without a cycle.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rebar_reconciler._backend import BackendAssigneeNotFoundError
from rebar_reconciler.batch_dispatch import create_one, delete_one, update_one
from rebar_reconciler.pass_io import (
    _load_alert_store,
    _load_conflict_resolver,
    _persist_field_provenance,
)

logger = logging.getLogger(__name__)


def _rebar_env(name: str, default: str | None = None) -> str | None:
    """Read ``REBAR_<name>`` from the environment.

    Local to this module (each reconciler module keeps its own copy): the
    reconciler modules are spec-loaded under test (where ``rebar_reconciler`` is the
    test-package shadow), so a cross-module import of a shared shim would not resolve.
    """
    return os.environ.get(f"REBAR_{name}", default)


@dataclass
class BatchApplyContext:
    """Mutable per-pass context threaded through the per-action handlers.

    The sequencer (``applier._apply_batch``) owns one instance per batch. Handlers
    read the resolved transport (``client``) and pass metadata, and mutate the
    running ``rest_calls`` budget plus the ``deferred_creates`` / ``events_list``
    accumulators that ``create_one`` appends to. The assignee soft-fail path catches
    the vendor-neutral ``BackendAssigneeNotFoundError`` base (ticket 97f2/bbf1) — the
    adapter's concrete assignee error subclasses it — so the core handler names no
    vendor error.
    """

    client: Any
    repo_root: Path
    pass_id: str
    binding_store: Any = None
    deferred_creates: list[dict] = field(default_factory=list)
    events_list: list[dict] = field(default_factory=list)
    rest_calls: int = 0


@dataclass
class HandlerResult:
    """Outcome of dispatching one mutation.

    ``outcome`` is the dict recorded in the manifest. ``soft_failed`` marks the
    update 404 / assignee-unresolved per-mutation soft-failures so the sequencer's
    RECON line omits the sub-op telemetry suffix — those paths record and return
    before that telemetry is computed, matching the pre-split output exactly.
    """

    outcome: dict
    soft_failed: bool = False


def handle_create(mutation: dict, ctx: BatchApplyContext) -> HandlerResult:
    """Dispatch an outbound CREATE via ``create_one`` and assemble its outcome."""
    outcome = dict(mutation)
    # Bug ea6d-e4b2-a316-45ec: collect any add_comment failures so a swallowed
    # comment sub-mutation during an outbound CREATE surfaces in the batch
    # outcome rather than reporting a clean error=None, mirroring the update-path
    # handling below (bug 6afc).
    comment_errors: list[str] = []
    result = create_one(
        mutation,
        ctx.client,
        rest_calls=ctx.rest_calls,
        deferred_creates=ctx.deferred_creates,
        events_list=ctx.events_list,
        repo_root=ctx.repo_root,
        binding_store=ctx.binding_store,
        comment_errors=comment_errors,
    )
    # Only count REST call on actual create (not dedup-skipped, not deferred)
    if result is not None and result.get("status") != "dedup-create-skipped":
        ctx.rest_calls += 1
    outcome["result"] = result
    # Surface swallowed comment failures. NON-fatal by default — the issue create
    # above genuinely succeeded — so we record them in a dedicated field rather than
    # overwriting outcome["error"], mirroring the update-path soft-fail style.
    if comment_errors:
        outcome["comment_errors"] = list(comment_errors)
        # Observability (48c8-5375-f883-462d): a swallowed comment failure is a
        # silent sub-op no-op. Behind the SAME fail-loud flag as the silent-no-op
        # canary in handle_update, promote it to a per-mutation error so it counts
        # toward mutation_failures (reconcile.py) and drives a non-zero pass exit.
        # Default off ⇒ landing is behavior-neutral; promotion/reversion are a pure
        # flag flip.
        if _rebar_env("RECONCILER_FAIL_SILENT_NOOP", "0") == "1":
            outcome["error"] = f"comment-errors: {'; '.join(comment_errors)}"
    return HandlerResult(outcome)


def handle_update(mutation: dict, ctx: BatchApplyContext) -> HandlerResult:
    """Dispatch an outbound UPDATE via ``update_one``, applying the per-mutation
    soft-fail, sub-op telemetry, silent-no-op canary, and provenance contracts.
    """
    outcome = dict(mutation)
    # Bug 17b5-dda4-6662-4616: AssigneeNotFoundError (raised by
    # client.update_issue's Phase A pre-validation when the local assignee
    # doesn't map to a real Jira account, e.g. 'Worktree' git-config default)
    # was killing the entire batch because the surrounding try-block only
    # handles HeadDriftError. Soft-fail this mutation: record an alert, mark
    # outcome error, and continue with the rest. Mirrors the existing
    # 400-illegal-transition fallback in update_one and the BRIDGE_ALERT pattern
    # in create_one.
    # Bug 6afc-20ee-84e5-4dd5: collect any add_comment failures so a swallowed
    # comment sub-mutation surfaces in the batch outcome rather than reporting a
    # clean error=None.
    _comment_errors: list[str] = []
    _subop: dict[str, int] = {}
    try:
        result = update_one(
            mutation,
            ctx.client,
            comment_errors=_comment_errors,
            subop_applied=_subop,
        )
    except urllib.error.HTTPError as exc:
        # Bug tan-coin-atone (6614-43cd-3a48-4f63): an outbound update against a
        # DELETED Jira issue (stale binding, 1e08 class) routes status/priority
        # through REST sub-calls (transition_issue / update_priority) that raise
        # a RAW urllib.error.HTTPError 404 — NOT a JiraAPIError — so the
        # update_one comment-fallback try/except (which only handles
        # JiraAPIError) misses it and the 404 escapes reconcile_once, aborting
        # the whole pass (GHA run 27023829257). A 404 on a single mutation's
        # target means the issue is gone: this is a PER-MUTATION failure, never
        # pass-fatal. Soft-fail ONLY 404 — other HTTP errors (e.g. 5xx) keep
        # current behavior and propagate (matching delete_one's already-gone
        # tolerance and the AssigneeNotFoundError soft-fail below). Positive-404
        # evidence feeds the binding-GC design in
        # docs/designs/sync-hardening-proposal.md Item 4b.
        if exc.code != 404:
            raise
        _outcome_key = mutation.get("key") or mutation.get("local_id") or "<unknown>"
        logger.warning(
            "outbound update skipped: Jira issue %s gone (HTTP 404) "
            "— stale binding (1e08); recording per-mutation failure "
            "and continuing the pass",
            _outcome_key,
        )
        outcome["result"] = None
        outcome["error"] = f"stale-binding-404: {exc!s}"
        return HandlerResult(outcome, soft_failed=True)
    except BackendAssigneeNotFoundError as exc:
        alert_store = _load_alert_store()
        alert_store.append(
            {
                "kind": "outbound-update-assignee-unresolved",
                "key": mutation.get("key"),
                "local_id": mutation.get("local_id"),
                "assignee": ((mutation.get("fields") or {}).get("assignee")),
                "pass_id": ctx.pass_id,
                "timestamp_ns": time.time_ns(),
                "reason": str(exc),
            },
            repo_root=ctx.repo_root,
        )
        outcome["result"] = None
        outcome["error"] = f"assignee-unresolved: {exc!s}"
        return HandlerResult(outcome, soft_failed=True)
    outcome["result"] = result
    # Bug 6afc-20ee-84e5-4dd5: surface swallowed comment failures. NON-fatal —
    # the scalar update above genuinely succeeded — so we record them in a
    # dedicated field rather than overwriting outcome["error"], mirroring the
    # soft-fail style of the stale-binding-404 / assignee-unresolved handlers.
    if _comment_errors:
        outcome["comment_errors"] = list(_comment_errors)
        # Observability (48c8-5375-f883-462d): behind the SAME fail-loud flag as the
        # silent-no-op canary below, promote a swallowed comment failure to a
        # per-mutation error so it counts toward mutation_failures (reconcile.py) and
        # drives a non-zero pass exit. Default off ⇒ behavior-neutral. If the
        # silent-no-op canary below ALSO fires, it augments (does not silently
        # replace) this reason.
        if _rebar_env("RECONCILER_FAIL_SILENT_NOOP", "0") == "1":
            outcome["error"] = f"comment-errors: {'; '.join(_comment_errors)}"
    # Story E (2359): sub-op telemetry — surface per-kind APPLIED counts on the
    # structured outcome (parity with apply_inbound's links_applied), so a
    # link/comment/label that silently no-ops is queryable, not only logged.
    outcome["labels_applied"] = _subop.get("labels_applied", 0)
    outcome["comments_applied"] = _subop.get("comments_applied", 0)
    outcome["links_applied"] = _subop.get("links_applied", 0)
    # Silent-no-op canary: a kind with sub-ops COMPUTED (post-dedup) but ZERO
    # applied is exactly the bug-3f04 link-drop failure mode — it would otherwise
    # pass green with error=None. computed is counted post-dedup, so an
    # idempotent re-sync (everything deduped) is computed==0 and does NOT fire.
    # NOTE: this is a TOTAL-no-op detector (applied==0) per the AC's
    # `computed > 0 && applied == 0` invariant — a PARTIAL drop (e.g. 2 links
    # computed, 1 applied) does not fire; a finer per-sub-op threshold is
    # deliberately out of scope (YAGNI).
    _silent = [
        kind
        for kind in ("labels", "comments", "links")
        if _subop.get(f"{kind}_computed", 0) > 0 and _subop.get(f"{kind}_applied", 0) == 0
    ]
    if _silent:
        _noop_key = mutation.get("key") or mutation.get("local_id") or "<unknown>"
        _detail = ", ".join(
            f"{k}: computed={_subop.get(f'{k}_computed', 0)} applied=0" for k in _silent
        )
        outcome["silent_noop"] = _silent
        logger.warning(
            "outbound update silent no-op for %s — %s; sub-ops were "
            "computed but NONE applied (the bug-3f04 failure mode)",
            _noop_key,
            _detail,
        )
        # Warn-first rollout: hard-fail (record a per-mutation failure) ONLY
        # behind the flag — promotion to hard-fail and reversion to warn are a
        # flag flip with no other code change. Augment (never silently replace) a
        # comment-errors reason already recorded above (48c8) so both sub-op
        # failures survive on the same outcome.
        if _rebar_env("RECONCILER_FAIL_SILENT_NOOP", "0") == "1":
            _prior = outcome.get("error")
            outcome["error"] = (
                f"{_prior}; silent-noop: {_detail}" if _prior else f"silent-noop: {_detail}"
            )
    # Persist provenance for set-valued fields after update
    jira_key = mutation.get("key", "")
    if jira_key:
        conflict_resolver = _load_conflict_resolver()
        mapping_path = ctx.repo_root / "bridge_state" / "mapping.json"
        for field_name, field_value in mutation.get("fields", {}).items():
            if conflict_resolver.FIELD_CLASSES.get(field_name) == "set":
                _persist_field_provenance(mapping_path, jira_key, field_name, field_value)
    return HandlerResult(outcome)


def handle_delete(mutation: dict, ctx: BatchApplyContext) -> HandlerResult:
    """Dispatch an outbound DELETE via ``delete_one`` (already-gone tolerated)."""
    outcome = dict(mutation)
    delete_one(mutation, ctx.client)
    outcome["result"] = None
    return HandlerResult(outcome)


def handle_unknown(mutation: dict, ctx: BatchApplyContext) -> HandlerResult:
    """Record the legacy unrecognised-action error outcome (the ``else`` arm)."""
    outcome = dict(mutation)
    action = mutation.get("action", "")
    outcome["result"] = None
    outcome["error"] = f"unknown action: {action!r}"
    return HandlerResult(outcome)


# Per-action dispatch table. Unrecognised actions fall through to handle_unknown
# (the legacy ``else`` arm). Keyed by the mutation's "action" field.
_ACTION_HANDLERS = {
    "create": handle_create,
    "update": handle_update,
    "delete": handle_delete,
}


def dispatch_mutation(mutation: dict, ctx: BatchApplyContext) -> HandlerResult:
    """Route one mutation dict to its per-action handler and return the result."""
    action = mutation.get("action", "")
    handler = _ACTION_HANDLERS.get(action, handle_unknown)
    return handler(mutation, ctx)


def record_backstop_failure(
    mutation: dict, exc: Exception, action: str, ctx: BatchApplyContext
) -> HandlerResult:
    """Per-mutation failure backstop, shared with the applier's dispatch loop.

    Generalizes the enumerated soft-fails above (400-comment / 404 / assignee /
    gone-delete): a mutation whose dispatch raised an *unhandled* exception — e.g.
    ``acli.transition_issue_by_name`` raising a bare ``RuntimeError`` for an
    unreachable Jira transition — is recorded as a per-mutation failure (a
    ``bridge_alerts`` entry + an outcome carrying an ``"error"`` key, which counts as
    a ``mutation_failure`` in reconcile's manifest tally) instead of propagating and
    aborting the whole pass. The caller re-raises the control-flow / fail-fast
    contracts (HeadDriftError / RescheduleError / non-404 HTTPError) before reaching
    here, so only genuine per-mutation failures land in this backstop.
    """
    key = mutation.get("key") or mutation.get("local_id") or "<unknown>"
    _load_alert_store().append(
        {
            "kind": "mutation-error",
            "key": mutation.get("key"),
            "local_id": mutation.get("local_id"),
            "action": action,
            "pass_id": ctx.pass_id,
            "timestamp_ns": time.time_ns(),
            "reason": str(exc),
        },
        repo_root=ctx.repo_root,
    )
    logger.warning("outbound %s on %s failed (%s) — recorded, continuing", action, key, exc)
    outcome = {**mutation, "action": action, "result": None, "error": f"mutation-error: {exc!s}"}
    return HandlerResult(outcome, soft_failed=True)
