#!/usr/bin/env python3
"""run_differs.py — the diff phase of a reconcile pass, extracted from reconcile.py.

``run_differs(ctx, route_inbound_probe)`` is the single ``reconcile_once`` phase
that was previously the in-file ``_run_differs`` helper. It runs the structural
invariants, the legacy snapshot diff, the inbound-probe dispatch, the outbound
differ (with OutboundMutation -> typed Mutation conversion), and the binding-aware
inbound differ (with InboundMutation -> typed Mutation conversion), accumulating the
typed-Mutation list onto the shared ``_PassContext`` (``ctx.mutations``).

Loader convention: like every sibling in this package, run_differs loads its own
siblings (``mutation.py`` / ``inbound_probe.py``) by file path via the local
``_load`` helper (``importlib.util.spec_from_file_location``), so it resolves both
under the real package and when a single module is loaded standalone in tests. It
holds NO back-edge to reconcile.py: the probe router (``route_inbound_probe``,
which reconcile.py keeps because it is a separately-tested public surface) is passed
in as a parameter, and ``ctx`` is typed loosely to avoid importing ``_PassContext``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

# ``lazy_load`` centralizes the by-path sibling-loader idiom (rebar_reconciler/
# _loader.py). Import it normally when package context exists, else bootstrap it
# by file path — this module is itself exec'd standalone via
# spec_from_file_location in tests.
try:
    from rebar_reconciler._loader import lazy_load
except ImportError:  # standalone load without package context
    _loader_key = "rebar_reconciler._loader"
    if _loader_key not in sys.modules:
        _loader_spec = importlib.util.spec_from_file_location(
            _loader_key, Path(__file__).parent / "_loader.py"
        )
        assert _loader_spec is not None and _loader_spec.loader is not None
        _loader_mod = importlib.util.module_from_spec(_loader_spec)
        sys.modules[_loader_key] = _loader_mod
        _loader_spec.loader.exec_module(_loader_mod)  # type: ignore[union-attr]
    lazy_load = sys.modules[_loader_key].lazy_load


def _load(name: str, relpath: str):
    """Load a sibling module by relative file path, registering it in sys.modules.

    Returns the cached module when ``name`` is already in ``sys.modules``;
    this allows test fixtures to pre-register patched modules and have
    ``run_differs`` reuse them rather than loading fresh copies. Delegates to the
    shared ``lazy_load`` helper (the package-wide by-path loader, mirrored by
    reconcile.py).
    """
    return lazy_load(name, relpath)


def _emit_outbound_field_alerts(
    conflict_sink: list[tuple[str, str]],
    dropped_field_sink: list[tuple[str, str]],
    repo_root: Path,
    pass_id: str,
) -> None:
    """Emit deduped bridge alerts for outbound both-sides conflicts (a713) and
    allowlist-dropped fields (acd0), mirroring fetcher._flag_unmapped_statuses.

    Deduped per (kind, ticket, field) over the alert_store 24h window so a persistent
    condition does not re-file every pass. Fully fail-open — any error is swallowed so
    observability can never break a reconcile pass."""
    if not conflict_sink and not dropped_field_sink:
        return
    try:
        alert_store = _load("rebar_reconciler.alert_store", "alert_store.py")
    except Exception:  # noqa: BLE001 — observability only; never break the pass
        return
    for kind, sink in (
        ("outbound-field-conflict", conflict_sink),
        ("outbound-field-dropped", dropped_field_sink),
    ):
        for jira_key, field in sink:
            dedup_key = f"{kind}:{jira_key}:{field}"
            try:
                if not alert_store.is_deduped(dedup_key, repo_root=repo_root):
                    alert_store.append(
                        {
                            "kind": kind,
                            "key": dedup_key,
                            "jira_key": jira_key,
                            "field": field,
                            "pass_id": pass_id,
                            "timestamp_ns": time.time_ns(),
                        },
                        repo_root=repo_root,
                    )
            except Exception:  # noqa: BLE001 — best-effort alert write
                pass


def _emit_recovery_failure_alerts(
    failures: list[dict[str, Any]],
    repo_root: Path,
    pass_id: str,
) -> None:
    """Emit a deduped bridge alert per pending-binding recovery failure (story 9622).

    Recovery was previously silently swallowed; a failure (a failed Jira search or
    retro-label attach) now files an ``outbound-recovery-failure`` alert, deduped per
    local_id over the alert_store 24h window. Fully fail-open — any error is swallowed
    so observability can never break a reconcile pass."""
    if not failures:
        return
    try:
        alert_store = _load("rebar_reconciler.alert_store", "alert_store.py")
    except Exception:  # noqa: BLE001 — observability only; never break the pass
        return
    for failure in failures:
        local_id = failure.get("local_id", "<unknown>")
        dedup_key = f"outbound-recovery-failure:{local_id}"
        try:
            if not alert_store.is_deduped(dedup_key, repo_root=repo_root):
                alert_store.append(
                    {
                        "kind": "outbound-recovery-failure",
                        "key": dedup_key,
                        "local_id": local_id,
                        "reason": failure.get("reason"),
                        "pass_id": pass_id,
                        "timestamp_ns": time.time_ns(),
                    },
                    repo_root=repo_root,
                )
        except Exception:  # noqa: BLE001 — best-effort alert write
            pass


def _read_local_ticket_full(repo_root: Path, local_id: str, *, no_sync: bool) -> dict | None:
    """Targeted read of ONE local ticket (including archived) via ``rebar show``.

    The binding-store acting walk (epic 3006-e198) reconciles bound pairs whose
    local ticket has left the ACTIVE snapshot (archived/deleted — ``rebar list``
    omits them), so it must read those tickets individually. ``rebar show`` reads
    any ticket regardless of archive state. Returns the ticket dict, or ``None``
    when the ticket no longer exists (hard-deleted) or the read fails (fail-open:
    the walk treats ``None`` as ``LocalState.ABSENT``). ``no_sync=True`` suppresses
    the tickets-branch fetch so a no-write pass stays no-write on the git tree.
    """
    import os as _os
    import subprocess as _sp

    from rebar._engine import in_process_cli

    cli = Path(in_process_cli())
    if not cli.exists():
        return None
    _env = dict(_os.environ, REBAR_SYNC_PULL="off") if no_sync else None
    try:
        result = _sp.run(
            # ``--include-provenance`` keeps ``managed_refs`` (the monotonic
            # removal-sync projection) in the output — the outbound differ reads it
            # to decide REMOVE-vs-ADOPT. It is stripped from the default human view,
            # so the reconciler MUST always pass this flag here.
            [str(cli), "show", "--include-provenance", local_id],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=30,
            env=_env,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — fail-open: a failed read → ABSENT (no action)
        return None


def run_differs(ctx: Any, route_inbound_probe: Callable[..., list[Any] | None]) -> None:
    """Diff phase: invariants + the legacy snapshot diff + inbound-probe dispatch +
    the outbound differ (with OM->Mutation conversion) + the binding-aware inbound
    differ (with IM->Mutation conversion). Accumulates the typed-Mutation list.

    ``ctx`` is the shared ``reconcile._PassContext`` (typed loosely as ``Any`` so
    this module holds no import edge back to reconcile.py). ``route_inbound_probe``
    is reconcile.py's probe router, injected as a parameter for the same reason.

    A thin orchestrator over the named ``_run_differs_*`` phase helpers, each of
    which captures one cohesive stage of the former inline body VERBATIM.
    """
    skip_invariant_filing, quarantine_keys, seed_repair_property_mutations = (
        _run_differs_invariants(ctx)
    )

    # Compute mutations (pure function, no I/O). The invariant signals are
    # passed through so the differ honors quarantine + seed mutations.
    mutations = ctx.differ.compute_mutations(
        ctx.prev_snapshot,
        ctx.curr_snapshot,
        quarantine_set=quarantine_keys,
        seed_mutations=seed_repair_property_mutations,
    )

    _run_differs_report_schema_drift(mutations, skip_invariant_filing, ctx.invariants_mod)
    _run_differs_inbound_probe_dispatch(mutations, route_inbound_probe)
    outbound_raw, absent_alive_fields, outbound_diff_client = _run_differs_outbound(ctx, mutations)
    _run_differs_inbound(ctx, mutations, outbound_raw, absent_alive_fields)
    _run_differs_binding_walk(ctx, mutations, outbound_diff_client)

    ctx.mutations = mutations


def _run_differs_invariants(ctx: Any) -> tuple[bool, set[str], list]:
    """Invariant phase: at-most-one-local-id filing + dual-identity round-trip.

    Returns ``(skip_invariant_filing, quarantine_keys, seed_repair_property_mutations)``
    — the ``skip_invariant_filing`` gate reused by later phases to suppress bug-filing
    side effects, plus the quarantine set + seed mutations the differ honors.
    """
    persist = ctx.persist
    filter_local_ids = ctx.filter_local_ids
    repo_root = ctx.repo_root
    invariants_mod = ctx.invariants_mod
    prev_snapshot = ctx.prev_snapshot
    curr_snapshot = ctx.curr_snapshot

    # Check structural invariants on the post-fetch snapshot, before diffing.
    # check_at_most_one_local_id returns only the filed violations (capped
    # at 5 per pass — see invariants._CAP_PER_PASS), so the prior log line's
    # "violations" and "filed" numbers were identical by construction. F11: log
    # filed count with the cap for clarity.
    #
    # Filtered passes skip invariant bug-filing to avoid side effects on
    # pre-existing violations outside the test scope.
    # Compose the bug-filing gate: invariant checks FILE local bug tickets
    # (CREATE events), so they must be skipped both for filtered passes (scope
    # leak) and for cap-0 no-write passes (ticket yaw-plait-doe).
    skip_invariant_filing = (not persist) or bool(filter_local_ids)
    if skip_invariant_filing:
        _reason = "no-write mode" if not persist else "filtered pass"
        # Diagnostic line → stderr so no-write mode keeps STDOUT a pure JSON
        # payload (the computed plan) for library/MCP callers.
        print(  # noqa: T201
            f"invariants: skipped ({_reason}, {len(curr_snapshot)} issues in snapshot)",
            file=sys.stderr,
        )
    else:
        filed = invariants_mod.check_at_most_one_local_id(curr_snapshot, repo_root=repo_root)
        print(  # noqa: T201
            f"invariants: scanned={len(curr_snapshot)} filed={len(filed)} (cap=5)"
        )

    # Invariant phase: verify dual-identity round-trip on the post-fetch
    # snapshot before diffing. Quarantine one-sided keys (skipped by the
    # differ) and seed repair_property mutations for one-sided local_id
    # rows so the differ emits the repair in this same pass.
    #
    # Filtered passes skip this to avoid seeding repair mutations for
    # non-test tickets that would leak outside the filter scope.
    if skip_invariant_filing:
        quarantine_keys: set[str] = set()
        seed_repair_property_mutations: list = []
    else:
        quarantine_keys, seed_repair_property_mutations = (
            invariants_mod.check_dual_identity_complete(prev_snapshot, curr_snapshot)
        )
    return skip_invariant_filing, quarantine_keys, seed_repair_property_mutations


def _run_differs_report_schema_drift(mutations, skip_invariant_filing, invariants_mod) -> None:
    """Post-emit phase: file dedup'd bug tickets for repair_property schema-drift follow-ons."""
    # Post-emit filter: scan mutations for repair_property follow-ons that carry a
    # schema-drift kind. report_schema_drift surfaces each drift (files a dedup'd bug
    # ticket + writes an alert record) so the signal is not swallowed.
    #
    # CONTRACT NOTE (aligned under meta-bug 5f2a-9a9f-2b4a-4aab): this loop matches the
    # ACTUAL kind that apply_inbound.inbound_repair_property emits on a repair failure —
    # "schema_drift_signal" (see apply_inbound.py, inbound_repair_property). It PREVIOUSLY
    # matched "schema_drift", a string no producer in src/ ever emits, so the in-pass
    # repair_property repair-failure follow-ons were silently dropped here. The issue key
    # is read from the emitter's "issue_key" field, with a "target" fallback for the
    # observed/expected-shaped follow-ons.
    #
    # Mutations may be plain dicts (legacy schema) or Mutation dataclass
    # instances (canonical contract from epic 4047 / cde1). Normalise on
    # access — mirrors the same dual-shape pattern in
    # preflight_status_mapping below. Pre-fix this loop used
    # `_m.get("action")` which crashed with "'Mutation' object has no
    # attribute 'get'" once the reconciler reached this code path in
    # production with typed Mutations.
    mut_mod_for_action = _load("reconcile_mutation", "mutation.py")
    for _m in mutations:
        action_attr = getattr(_m, "action", None)
        if action_attr is not None:
            # Typed Mutation shape. Normalise enum/string for comparison.
            action_str = getattr(action_attr, "value", action_attr)
            payload = getattr(_m, "payload", None) or {}
            follow_on = payload.get("follow_on") if isinstance(payload, Mapping) else None
        else:
            # Legacy dict shape.
            action_str = _m.get("action")
            follow_on = _m.get("follow_on")
        if action_str != mut_mod_for_action.MutationAction.repair_property.value:
            continue
        if isinstance(follow_on, Mapping) and follow_on.get("kind") == "schema_drift_signal":
            # report_schema_drift FILES a local bug ticket + writes an alert
            # record (CREATE events). Skip in no-write / filtered passes.
            if skip_invariant_filing:
                continue
            invariants_mod.report_schema_drift(
                follow_on.get("issue_key") or follow_on.get("target"),
                follow_on.get("observed"),
                follow_on.get("expected"),
            )


def _run_differs_inbound_probe_dispatch(mutations, route_inbound_probe) -> None:
    """Inbound-probe dispatch phase: route (inbound, probe) mutations, append follow-ons."""
    # Inbound-probe dispatch: any (inbound, probe) Mutation emitted by the
    # differ is routed through the live inbound_probe classifier, then
    # converted into a branch-specific follow-on (or a log-only outcome) via
    # route_inbound_probe. Follow-on mutations are appended in-place so the
    # applier dispatches them in the same pass.
    mut_mod = _load("reconcile_mutation", "mutation.py")
    probe_mod = _load("inbound_probe", "inbound_probe.py")
    probe_follow_ons: list = []
    for _m in mutations:
        # Only Mutation objects with the (inbound, probe) combo trigger a probe.
        direction = getattr(_m, "direction", None)
        action = getattr(_m, "action", None)
        if direction is None or action is None:
            continue
        if direction != mut_mod.MutationDirection.inbound:
            continue
        if action != mut_mod.MutationAction.probe:
            continue
        try:
            probe_result = probe_mod.probe(_m.target)
        except probe_mod.ProbeConfigError as exc:
            # Missing env → treat as unreachable; do not abort the pass.
            print(  # noqa: T201
                f"inbound_probe: skipped key={_m.target} reason=config_error err={exc}",
                file=sys.stderr,
            )
            continue
        follow_ons = route_inbound_probe(_m, probe_result)
        if follow_ons:
            probe_follow_ons.extend(follow_ons)
    if probe_follow_ons:
        mutations.extend(probe_follow_ons)


def _run_differs_outbound(ctx: Any, mutations) -> tuple[list, dict, Any]:
    """Outbound differ phase: recover bindings, compute label intent + the outbound
    differ, and convert each OutboundMutation -> typed Mutation onto ``mutations``.

    Returns ``(outbound_raw, absent_alive_fields, outbound_diff_client)`` for the
    inbound differ + binding-walk phases that follow.
    """
    filter_local_ids = ctx.filter_local_ids
    binding_store = ctx.binding_store
    local_tickets = ctx.local_tickets
    local_label_intent_mod = ctx.local_label_intent_mod
    tracker_dir = ctx.tracker_dir
    repo_root = ctx.repo_root
    outbound_differ_mod = ctx.outbound_differ_mod
    pass_id = ctx.pass_id
    prev_snapshot = ctx.prev_snapshot
    curr_snapshot = ctx.curr_snapshot
    sync_logger = ctx.sync_logger
    mut_mod = _load("reconcile_mutation", "mutation.py")

    # -------------------------------------------------------------------
    # Outbound differ: local → Jira mutations via binding store.
    #
    # Recover any pending bindings from prior failed passes, then compute
    # outbound mutations from local tickets vs. Jira snapshot. Each
    # OutboundMutation is converted to a typed Mutation so it flows through
    # the unified applier.apply() dispatch (cap enforcement, direction-aware
    # routing).
    # -------------------------------------------------------------------
    # Build the AcliClient BEFORE recovery (story 9622) so recover_pending_bindings
    # gets a real client exposing search_issues/add_label/set_entity_property — it
    # was previously (mis)passed the `applier` MODULE (no search_issues), so every
    # recovery AttributeError'd into the fail-open swallow below and NEVER ran. The
    # same client is reused by the outbound differ's live-comment fetch further down.
    # Built via the stable acli_subprocess floor (acli_mod_for_comments may be a test
    # fake that only provides AcliClient). Deferred to runtime (not import) so the
    # differ stays importable without JIRA_URL/JIRA_USER set.
    from rebar_reconciler import acli as acli_mod_for_comments
    from rebar_reconciler import acli_subprocess

    _s = acli_subprocess.resolve_jira_settings()
    outbound_diff_client = acli_mod_for_comments.AcliClient(
        jira_url=_s.url, user=_s.user, api_token=_s.api_token
    )

    # Filtered passes skip pending-binding recovery to avoid finalizing
    # bindings for non-test tickets (scope leak).
    ctx.recovery_failures = 0
    if not filter_local_ids:
        recovery_failures: list[dict[str, Any]] = []
        try:
            binding_store.recover_pending_bindings(
                outbound_diff_client, failure_sink=recovery_failures
            )
        except Exception as exc:  # noqa: BLE001 — fail-open: a total recovery failure is non-fatal
            recovery_failures.append({"local_id": "<all>", "reason": repr(exc)})
            print(  # noqa: T201
                f"reconcile: binding recovery failed ({exc}), continuing",
                file=sys.stderr,
            )
        # LOUD (story 9622): surface per-entry failures instead of the silent
        # swallow — a deduped bridge alert each + a nonzero recovery_failures tally.
        if recovery_failures:
            ctx.recovery_failures = len(recovery_failures)
            _emit_recovery_failure_alerts(recovery_failures, repo_root, pass_id)

    # Bug a06c: compute per-binding label-intent map BEFORE the differ
    # runs. The outbound differ uses it to gate REMOVE emission so that
    # labels Jira added side-band (which local never had) do not produce
    # spurious REMOVEs — those spurious REMOVEs cancel legitimate
    # inbound ADDs under the PR #457 local-wins bidir suppression
    # contract, silently dropping the label on both sides (the T3 IB-ADD
    # probe failure). Only bound tickets need intent; unbound tickets
    # emit creates with their full tag set unconditionally.
    bound_local_ids = [
        t.get("ticket_id", t.get("id", ""))
        for t in local_tickets
        if binding_store.get_jira_key(t.get("ticket_id", t.get("id", ""))) is not None
    ]
    local_label_intent = local_label_intent_mod.compute_label_intent_map(
        bound_local_ids, tracker_dir
    )

    # Note: `outbound_diff_client` (the AcliClient used here by the outbound
    # differ's live-comment fetch — Bug 4292) is now built ABOVE, before
    # pending-binding recovery, so recovery can reuse it (story 9622).

    # Bug 0702-3b6d-c1db-4ed3 (inbound counterpart to 1e08): the outbound differ
    # RETURNS the bound-but-absent ALIVE direct-GET results (each alive HTTP-200
    # absent key's raw fields) as the second element of its tuple, so the inbound
    # differ can mirror Jira-side changes for out-of-window keys WITHOUT a second
    # GET. We merge them into the inbound snapshot below. 404/transport keys are
    # intentionally absent from this dict (retirement stays outbound-owned).
    # Observability sinks (bugs a713/acd0): the differ appends (jira_key, field) for a
    # both-sides field conflict (local-wins silently overwrites a Jira edit) and for a
    # mapped-but-allowlist-excluded field that differs (a silent outbound drop). We emit
    # deduped bridge alerts from them below — behavior is otherwise unchanged.
    conflict_sink: list[tuple[str, str]] = []
    dropped_field_sink: list[tuple[str, str]] = []
    # Story a118: read the Phase-3 consumer-swap flag ONCE per pass here (mirrors
    # the max_acting_fraction read below); fail-safe OFF if config is unreadable.
    try:
        from rebar.config import ConfigError, load_config

        _consumer_swap = bool(load_config().reconciler.baseline_consumer_swap)
    except ConfigError:
        _consumer_swap = False
    outbound_raw, absent_alive_fields = outbound_differ_mod.compute_outbound_mutations(
        local_tickets,
        curr_snapshot,
        binding_store,
        outbound_differ_mod.OutboundDiffConfig(
            excluded_statuses={"archived", "deleted"},
            local_label_intent=local_label_intent,
            client=outbound_diff_client,
            pass_id=pass_id,
            prev_snapshot=prev_snapshot,
            conflict_sink=conflict_sink,
            dropped_field_sink=dropped_field_sink,
            baseline_consumer_swap=_consumer_swap,
        ),
    )
    _emit_outbound_field_alerts(conflict_sink, dropped_field_sink, repo_root, pass_id)
    sync_logger.log(
        "outbound_differ_complete",
        count=len(outbound_raw),
    )
    # Bug b859 (Part 0c): structured per-direction breakdown to stderr so
    # operators / probes can see per-action counts without parsing the
    # sync_logger JSON manifest. Format: ``RECON: <kind> <field>=<value>``
    # with a stable token prefix that's distinct from FILTERED/filter/OK/
    # ERROR so the probe's grep filter does not need to be updated.
    _ob_creates = sum(1 for m in outbound_raw if m.action == "create")
    _ob_updates = sum(1 for m in outbound_raw if m.action == "update")
    _ob_deletes = sum(1 for m in outbound_raw if m.action == "delete")
    print(  # noqa: T201
        f"RECON: outbound_differ total={len(outbound_raw)} "
        f"create={_ob_creates} update={_ob_updates} delete={_ob_deletes}",
        file=sys.stderr,
    )

    # Convert OutboundMutation → typed Mutation for unified dispatch.
    for om in outbound_raw:
        if om.action == "create":
            typed = mut_mod.Mutation(
                direction=mut_mod.MutationDirection.outbound,
                action=mut_mod.MutationAction.create,
                target=om.local_id,
                payload={
                    **om.fields,
                    "comments": om.comments,
                    "labels": om.labels,
                    "local_id": om.local_id,
                },
                provenance={"source": "outbound_differ", "local_id": om.local_id},
            )
        elif om.action == "update":
            typed = mut_mod.Mutation(
                direction=mut_mod.MutationDirection.outbound,
                action=mut_mod.MutationAction.update,
                target=om.jira_key or om.local_id,
                payload={
                    "changed_fields": om.fields,
                    "comments": om.comments,
                    "labels": om.labels,
                    # Cycle 3: link adds ride the existing update payload
                    # (no new MutationAction) — _apply_outbound_update reads
                    # payload["links"] and calls client.set_relationship.
                    "links": getattr(om, "links", []),
                },
                provenance={
                    "source": "outbound_differ",
                    "local_id": om.local_id,
                    "jira_key": om.jira_key,
                },
            )
        elif om.action == "delete":
            typed = mut_mod.Mutation(
                direction=mut_mod.MutationDirection.outbound,
                action=mut_mod.MutationAction.delete,
                target=om.jira_key or om.local_id,
                payload={},
                provenance={
                    "source": "outbound_differ",
                    "local_id": om.local_id,
                    "jira_key": om.jira_key,
                },
            )
        else:
            continue  # unknown action — skip
        mutations.append(typed)
    return outbound_raw, absent_alive_fields, outbound_diff_client


def _run_differs_inbound(ctx: Any, mutations, outbound_raw, absent_alive_fields) -> None:
    """Inbound differ phase (binding-aware): Jira -> local for bound tickets;
    convert each InboundMutation -> typed Mutation onto ``mutations``."""
    local_tickets = ctx.local_tickets
    inbound_differ_mod = ctx.inbound_differ_mod
    binding_store = ctx.binding_store
    sync_logger = ctx.sync_logger
    curr_snapshot = ctx.curr_snapshot
    mut_mod = _load("reconcile_mutation", "mutation.py")

    # -------------------------------------------------------------------
    # Inbound differ (binding-aware): Jira → local for bound tickets.
    #
    # This coexists with the legacy snapshot-diff inbound path above. The
    # legacy path handles unbound Jira issues (create/delete/probe); this
    # path handles field-level updates for already-bound tickets.
    # -------------------------------------------------------------------
    local_by_id = {t.get("ticket_id", t.get("id", "")): t for t in local_tickets}
    # Bug 3bf8: pass outbound mutations so the inbound differ can suppress
    # emissions that would contradict (and clobber) a just-emitted outbound
    # change for the same target in the same bidirectional pass.
    # Bug 3bf8: ``compute_inbound_mutations`` returns
    # ``(mutations, suppression_count)`` — the count of inbound field/label
    # items dropped by bidirectional outbound-context filtering. Single-pass
    # to avoid O(2n) differ cost on every reconcile pass.
    # Bug 0702: merge the outbound differ's alive bound-but-absent GET results
    # into the snapshot the inbound differ sees, so out-of-window Jira issues
    # are mirrored Jira→local with the GET shared across both directions (no
    # double-GET). curr_snapshot is left unmutated (shallow copy) so downstream
    # snapshot persistence is unaffected.
    inbound_snapshot = curr_snapshot
    if absent_alive_fields:
        inbound_snapshot = dict(curr_snapshot)
        inbound_snapshot.update(absent_alive_fields)
    inbound_new, _ib_suppressed = inbound_differ_mod.compute_inbound_mutations(
        inbound_snapshot,
        binding_store,
        local_by_id,
        outbound_mutations=outbound_raw,
    )
    sync_logger.log(
        "inbound_differ_complete",
        count=len(inbound_new),
    )
    _ib_with_fields = sum(1 for m in inbound_new if m.fields)
    _ib_with_labels = sum(1 for m in inbound_new if m.labels)
    _ib_with_comments = sum(1 for m in inbound_new if getattr(m, "comments", []))
    print(  # noqa: T201
        f"RECON: inbound_differ total={len(inbound_new)} "
        f"with_fields={_ib_with_fields} with_labels={_ib_with_labels} "
        f"with_comments={_ib_with_comments}",
        file=sys.stderr,
    )
    print(  # noqa: T201
        f"RECON: bidir_suppressed inbound={_ib_suppressed}",
        file=sys.stderr,
    )

    # Convert InboundMutation → typed Mutation for unified dispatch.
    for im in inbound_new:
        typed = mut_mod.Mutation(
            direction=mut_mod.MutationDirection.inbound,
            action=mut_mod.MutationAction.update,
            target=im.jira_key,
            payload={
                "local_id": im.local_id,
                # Bug b859 (Part 1a, H3 fix): _apply_inbound_update reads
                # ``fields = payload.get("fields") or payload`` at
                # applier.py:625. Writing the inbound mutation's fields
                # under "changed_fields" (mirroring the outbound convention)
                # caused the .get("fields") lookup to miss → fallback to
                # the wrapper dict whose keys never matched the
                # summary/status/etc. branches → every inbound EDIT/STATUS
                # event was silently dropped → Phase 6 idempotency churn.
                # Use "fields" so the existing applier consumer finds the
                # dict on the first lookup.
                "fields": im.fields,
                "labels": im.labels,
                # Bug 85a1 (Gap 1): propagate inbound comments so each new
                # Jira comment is written as a local COMMENT event with
                # jira_comment_id binding. getattr default keeps backward
                # compat with InboundMutation variants (legacy test stubs)
                # that lack the field.
                "comments": getattr(im, "comments", []),
                # Cycle 3: inbound link adds — _apply_inbound_update writes
                # each into rebar via the rebar.link library facade.
                "links": getattr(im, "links", []),
            },
            provenance={
                "source": "inbound_differ",
                "jira_key": im.jira_key,
                "local_id": im.local_id,
            },
        )
        mutations.append(typed)


def _run_differs_binding_walk(ctx: Any, mutations, outbound_diff_client) -> None:
    """Binding-store-driven acting walk (drift classes A + C); extend ``mutations``."""
    repo_root = ctx.repo_root
    persist = ctx.persist
    local_tickets = ctx.local_tickets
    binding_store = ctx.binding_store
    curr_snapshot = ctx.curr_snapshot
    outbound_differ_mod = ctx.outbound_differ_mod
    sync_logger = ctx.sync_logger
    mut_mod = _load("reconcile_mutation", "mutation.py")

    # ── binding-store-driven acting walk (drift classes A + C; epic 3006-e198) ──
    # The differ above iterates the ACTIVE local snapshot, so a bound pair whose
    # local ticket has been archived/deleted (dropped from ``rebar list``) is
    # invisible to it. The level-triggered walk closes that blind spot: it iterates
    # the BINDING STORE, targeted-reads each off-snapshot local ticket, and drives
    # class A (terminal-transition → Done) + class C (probe → grace → GC) from one
    # shared iteration — breaker-gated before any mutation (13eb / 444d).
    binding_walk_mod = _load("reconcile_binding_walk", "binding_walk.py")
    classify_mod = _load("reconcile_classify", "classify.py")
    from rebar.config import ConfigError, load_config

    try:
        _max_acting_fraction = load_config().reconciler.max_acting_fraction
    except ConfigError:
        _max_acting_fraction = 0.10
    _active_local_ids = {t.get("ticket_id") for t in local_tickets if t.get("ticket_id")}
    # Keys the EDGE-triggered differ already emits an inbound-create for this pass
    # (new since prev_snapshot) — the level-triggered adopt must not double-create
    # them; it only heals steady-state misses (present in prev AND curr, unbound).
    _already_created = {
        getattr(m, "target", None)
        for m in mutations
        if getattr(m, "direction", None) == mut_mod.MutationDirection.inbound
        and getattr(m, "action", None) == mut_mod.MutationAction.create
        and getattr(m, "target", None)
    }
    walk = binding_walk_mod.compute_binding_walk_mutations(
        binding_store,
        curr_snapshot,
        _active_local_ids,
        client=outbound_diff_client,
        local_reader=lambda lid: _read_local_ticket_full(repo_root, lid, no_sync=not persist),
        max_acting_fraction=_max_acting_fraction,
        classify_mod=classify_mod,
        mutation_mod=mut_mod,
        outbound_differ_mod=outbound_differ_mod,
        persist=persist,
        skip_adopt_keys=_already_created,
    )
    sync_logger.log(
        "binding_walk_complete",
        acting=len(walk.mutations),
        retired=len(walk.retired),
        probed=len(walk.probed),
        alerted=len(walk.alerted),
        adopted=len(walk.adopted),
        breaker_allowed=bool(walk.breaker.allowed) if walk.breaker else True,
        refused=walk.refused,
    )
    # Story a118 census expand-contract: `walk.census` IS a `classify.census()`
    # record (binding_walk.py:239), so it is emitted under the canonical
    # `decision_census` event name (classify.py:351) — the documented-but-never-
    # emitted census now goes live. No consumer read the old `binding_walk_census`
    # key (grep-verified), so this is a direct rename. ROLLBACK (one line): revert
    # this event name to "binding_walk_census".
    sync_logger.log("decision_census", **walk.census)
    if walk.refused:
        sync_logger.log(
            "binding_walk_breaker_refused",
            reason=walk.breaker.reason if walk.breaker else "",
        )
    for _alert_key in walk.alerted:
        sync_logger.log("binding_walk_alert", jira_key=_alert_key)
    mutations.extend(walk.mutations)
