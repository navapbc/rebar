#!/usr/bin/env python3
"""Per-action mutation handlers for the legacy outbound batch path.

``applier._apply_batch`` is the *sequencer*: HEAD-drift recheck loop → dispatch →
record → manifest-write tail. This module owns the *per-action orchestration*
that wraps the ``batch_dispatch`` Jira-call primitives, one handler per
``MutationAction``:

    - ``handle_create`` — REST-budget counting + swallowed-comment surfacing
      around ``create_one``.
    - ``handle_update`` — the assignee-unresolved per-mutation soft-fail, the
      sub-op (labels/comments/links) telemetry, the bug-3f04 silent-no-op
      canary, and set-valued-field provenance, around ``update_one``.
    - ``handle_delete`` — ``delete_one`` (already-gone tolerance lives in the
      primitive).
    - ``handle_unknown`` — the legacy unrecognised-action error outcome.

All three dispatching handlers share ONE stale-binding-404 soft-fail
(``_soft_fail_stale_binding_404``, bug 449f-f9bf-be90-47fe): a Jira ``404`` on any
outbound mutation is per-mutation, never pass-fatal; every other ``HTTPError``
still propagates fail-fast to ``applier._apply_one``.

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
import sys
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rebar_reconciler import peer_state
from rebar_reconciler._backend import BackendAssigneeNotFoundError
from rebar_reconciler.batch_dispatch import create_one, delete_one, update_one
from rebar_reconciler.pass_io import (
    _load_alert_store,
    _load_conflict_resolver,
    _persist_field_provenance,
)

logger = logging.getLogger(__name__)


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
    # Bug e6e9: local_id -> the vendor-shaped fields whose outbound write CONFIRMEDLY
    # landed this pass. Consumed by reconcile._advance_baselines to advance the ADR-0026
    # baseline to the last-SYNCED value instead of the pass-start fetch. Only a handler
    # that saw its write complete may write here — see dispatch_one._update_one_scalar_update.
    synced_fields: dict[str, dict] = field(default_factory=dict)


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


def _soft_fail_stale_binding_404(
    mutation: dict, exc: urllib.error.HTTPError, action: str
) -> HandlerResult:
    """Record a Jira ``404`` on one mutation as a PER-MUTATION soft failure.

    Single source of truth for the stale-binding-404 contract, shared by all three
    outbound handlers (bug 449f-f9bf-be90-47fe). A 404 on a mutation's target means
    that Jira issue is gone (the 1e08 stale-binding class): the mutation itself
    failed, but the *pass* is still perfectly valid, so we record the failure and let
    the sequencer dispatch the rest of the batch. Only ``handle_update`` used to own
    this logic (bug tan-coin-atone / 6614-43cd-3a48-4f63); ``handle_create`` and
    ``handle_delete`` had NO try/except at all, so a 404 from either leaf escaped
    ``applier._apply_one`` and aborted the whole pass — GHA run 30465914822 applied
    1 of 30 planned mutations and silently skipped 29, violating bug
    e534-5154-2401-40fb's "no valid mutation is silently skipped" contract.

    Callers re-raise non-404 ``HTTPError``s themselves: 5xx and friends keep the
    fail-fast behavior that ``applier._apply_one``'s re-raise arm depends on.
    """
    key = mutation.get("key") or mutation.get("local_id") or "<unknown>"
    logger.warning(
        "outbound %s skipped: Jira issue %s gone (HTTP 404) "
        "— stale binding (1e08); recording per-mutation failure "
        "and continuing the pass",
        action,
        key,
    )
    outcome = dict(mutation)
    outcome["result"] = None
    outcome["error"] = f"stale-binding-404: {exc!s}"
    return HandlerResult(outcome, soft_failed=True)


def handle_create(mutation: dict, ctx: BatchApplyContext) -> HandlerResult:
    """Dispatch an outbound CREATE via ``create_one`` and assemble its outcome."""
    outcome = dict(mutation)
    # Bug ea6d-e4b2-a316-45ec: collect any add_comment failures so a swallowed
    # comment sub-mutation during an outbound CREATE surfaces in the batch
    # outcome rather than reporting a clean error=None, mirroring the update-path
    # handling below (bug 6afc).
    comment_errors: list[str] = []
    try:
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
    except urllib.error.HTTPError as exc:
        # Bug 449f-f9bf-be90-47fe: a 404 from the create leaf (e.g. a POST against a
        # project/parent that has been deleted, or a sub-call against a stale
        # binding) is a per-mutation failure, not a pass-fatal one. Return BEFORE the
        # rest_calls accounting below so a failed create never consumes REST budget.
        # Non-404 (5xx, 4xx-other) still propagates fail-fast, unchanged.
        if exc.code != 404:
            raise
        return _soft_fail_stale_binding_404(mutation, exc, "create")
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
        if os.environ.get("REBAR_RECONCILER_FAIL_SILENT_NOOP", "0") == "1":  # read-via: inject
            outcome["error"] = f"comment-errors: {'; '.join(comment_errors)}"
    return HandlerResult(outcome)


def _rich_cutover_active() -> bool:
    """Whether ANY client is cut over to the rich-text wire (story 3388).

    Reads ``reconciler.rich_text_cutover`` at CALL time, never at import, so
    flipping the flag needs no redeploy. Fails CLOSED — an unreadable or absent
    config, or a ``rebar`` package that is not importable at all (the engine ships
    as stdlib-only subprocess package data), all answer False — so a config fault
    can never silently switch this on.

    This deliberately does NOT call ``adapters.jira_family.rich_text.
    cutover_clients``, which answers the same question for the codecs. Core must
    not import the vendor package: the dependency direction is one-way (concrete
    backends import ``jira_family``; it never imports core back), and
    ``config.local_to_jira_status`` documents the same choice for the status map —
    a second independent read, kept honest by a PARITY TEST rather than by an
    import that would invert the layering.

    With the flag off (the default) every caller below is skipped, so the
    plain-wire behaviour is byte-identical to before the cutover shipped.
    """
    try:
        from rebar.config import ConfigError, compose_config
    except ImportError:
        return False
    try:
        return compose_config().reconciler.rich_text_cutover in ("cloud", "dc", "both")
    except (ConfigError, AttributeError):
        # AttributeError is in the set deliberately: the engine is loaded as package
        # data and can run against a rebar whose config predates this key, and callers
        # substitute partial config objects. Failing closed over a missing field beats
        # taking the apply path down over one we can default.
        return False


def _observe_rich_reemit(
    ctx: BatchApplyContext, local_id: str, jira_key: Any, fields_synced: dict
) -> None:
    """Bound a non-converging rich-text body with ONE post-push GET.

    The DC codec is one-way and lossy, so a body is not guaranteed to reach a
    codec fixed point: the differ can decide the local body still differs from the
    baseline, push an identical wire, and decide the same thing again next pass.
    Nothing in the plain-wire design detects that, because under a lossless codec
    it cannot happen.

    So each CONFIRMED description push is recorded against the binding
    (``peer_state.note_rich_emit``), and when the same wire has gone out
    ``RICH_REEMIT_OBSERVE_AT`` times in a row this reads the body back ONCE and
    overlays what Jira actually stored onto the fields this mutation synced. That
    overlay reaches the baseline the way every other confirmed write does — as a
    ``synced_fields`` entry consumed by ``reconcile._advance_baselines``, which
    calls ``binding_store.merge_baseline``. **This function never writes a
    baseline itself**; ``_advance_baselines`` remains the sole baseline writer, and
    that invariant is why the observation is expressed as an overlay rather than a
    direct ``set_baseline`` here.

    Fail-open throughout: a missing binding, a store predating the fields, or a
    failed GET leaves the pushed value in place, which is exactly today's
    behaviour. The cost is bounded to one extra REST call per divergence episode
    (the threshold is an equality, so the episode does not re-charge every pass).

    ``all_bindings`` rebuilds the outer mapping on each call, so this is O(bindings)
    per description push rather than a lookup. That is deliberate: it is the only
    PUBLIC way to reach a binding record, ``binding_store.py`` sits at the module-size
    cap and cannot carry a narrower accessor, and the work is a few thousand dict
    inserts against a REST round-trip in the same breath. If that file is ever split
    along a call-graph seam, give it a single-entry accessor and use it here.
    """
    wire = fields_synced.get("description")
    if wire is None or ctx.binding_store is None or not _rich_cutover_active():
        return
    try:
        entry = ctx.binding_store.all_bindings().get(local_id)
        if not isinstance(entry, dict):
            return
        if peer_state.note_rich_emit(entry, wire) != peer_state.RICH_REEMIT_OBSERVE_AT:
            return
        if not jira_key or ctx.client is None:
            return
        observed = ctx.client.get_issue_by_rest(str(jira_key)).get("fields", {}).get("description")
    except Exception as exc:  # noqa: BLE001 — best-effort observation; never break a pass
        print(f"RECON: rich_reemit_observe_failed key={jira_key} ({exc})", file=sys.stderr)
        return
    ctx.rest_calls += 1
    if observed is None:
        return
    ctx.synced_fields.setdefault(local_id, {})["description"] = observed
    print(f"RECON: rich_reemit_observed key={jira_key}", file=sys.stderr)


def _make_link_confirm(mutation: dict, ctx: BatchApplyContext):
    """Build the peer-confirmation sink for one outbound UPDATE (epic a4bd), or None.

    This is the PRODUCTION outbound-update path, and ``BatchApplyContext`` already
    carries everything the record needs (``binding_store``, ``pass_id``,
    ``repo_root``) — the same context-to-dispatch pattern ``synced_fields`` uses — so
    no new plumbing is threaded through ``update_one``.

    The store is keyed on LOCAL ids while the dispatched link entry carries JIRA
    keys, so the target is reverse-mapped here. An UNBOUND target records NOTHING: a
    Jira key must never be written into a local-id field, and the confirmation is
    picked up on a later pass or from a fetched snapshot instead.

    Returns ``None`` — disabling recording, i.e. exactly the pre-a4bd behaviour —
    whenever the context is incomplete or the store cannot be opened.
    """
    binding_store = ctx.binding_store
    local_id = mutation.get("local_id")
    if binding_store is None or not local_id:
        return None
    try:
        from rebar_reconciler.peer_confirmations import (
            DIRECTION_OUTBOUND,
            SOURCE_PUSH,
            open_store,
        )

        store = open_store(ctx.repo_root)
    except Exception as exc:  # noqa: BLE001 — fail-open: no evidence is worse, not fatal
        print(f"handle_update: peer-confirmation store unavailable: {exc!r}", file=sys.stderr)
        return None

    def _confirm(*, to_key, relation, link_id) -> None:
        if not to_key or not relation:
            return
        target_local_id = binding_store.get_local_id(to_key)
        if not target_local_id:
            return  # unbound target — nothing local to key the evidence on
        store.record(
            str(local_id),
            str(target_local_id),
            str(relation),
            link_id=link_id,
            direction=DIRECTION_OUTBOUND,
            pass_id=ctx.pass_id,
            source_kind=SOURCE_PUSH,
        )
        store.save()

    return _confirm


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
    # Bug e6e9: filled by update_one ONLY if the scalar write completed. Every arm below
    # that soft-fails the mutation returns before the record at the bottom, and the
    # record_backstop_failure path never reaches this function's tail at all, so a
    # mutation that did not land contributes nothing to the baseline advance.
    _fields_synced: dict[str, Any] = {}
    _link_confirm = _make_link_confirm(mutation, ctx)
    try:
        result = update_one(
            mutation,
            ctx.client,
            comment_errors=_comment_errors,
            subop_applied=_subop,
            fields_synced=_fields_synced,
            link_confirm=_link_confirm,
            binding_store=ctx.binding_store,
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
        # Bug 449f-f9bf-be90-47fe: the recording itself now lives in the shared
        # _soft_fail_stale_binding_404 helper so create/update/delete cannot drift
        # apart — the log line and the "stale-binding-404: …" error string are
        # byte-identical to what this arm emitted before the extraction.
        if exc.code != 404:
            raise
        return _soft_fail_stale_binding_404(mutation, exc, "update")
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
    # Bug e6e9: record what this mutation actually SYNCED, for the ADR-0026 baseline
    # advance. Reached only past every soft-fail return above, and _fields_synced is
    # itself empty unless client.update_issue completed — two independent gates, so
    # "the pass ran" can never be mistaken for "the write landed".
    _local_id = mutation.get("local_id")
    if _local_id and _fields_synced:
        ctx.synced_fields.setdefault(str(_local_id), {}).update(_fields_synced)
        _observe_rich_reemit(ctx, str(_local_id), mutation.get("key"), _fields_synced)
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
        if os.environ.get("REBAR_RECONCILER_FAIL_SILENT_NOOP", "0") == "1":  # read-via: inject
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
        if os.environ.get("REBAR_RECONCILER_FAIL_SILENT_NOOP", "0") == "1":  # read-via: inject
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
    try:
        delete_one(mutation, ctx.client)
    except urllib.error.HTTPError as exc:
        # Bug 449f-f9bf-be90-47fe: delete_one tolerates the already-gone case it can
        # SEE, but a raw urllib 404 from a REST sub-call escapes it — and with no
        # try/except here it escaped _apply_one too and killed the pass. A 404 on a
        # delete is the most benign failure there is (the target is already gone), so
        # record it per-mutation and continue; non-404 still propagates fail-fast.
        if exc.code != 404:
            raise
        return _soft_fail_stale_binding_404(mutation, exc, "delete")
    outcome["result"] = None
    return HandlerResult(outcome)


def handle_unknown(mutation: dict, _ctx: BatchApplyContext) -> HandlerResult:
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
    contracts (HeadDriftError / RescheduleError / HTTPError) before reaching here, so
    only genuine per-mutation failures land in this backstop. Since bug
    449f-f9bf-be90-47fe every handler soft-fails a 404 itself, so the HTTPError that
    reaches the caller's re-raise arm is always a non-404 one.
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
