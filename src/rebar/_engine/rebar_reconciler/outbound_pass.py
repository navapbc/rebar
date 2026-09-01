#!/usr/bin/env python3
"""outbound_pass.py — the outbound-differ sub-pass of the diff phase, extracted
from run_differs.py (ticket 7153-e5ad-5e20-4ae9, module-size headroom for the
typed-payload cutover).

``_run_differs_outbound(ctx, mutations, backend)`` is the self-contained outbound
sub-pass that ``run_differs.run_differs`` calls to recover pending bindings, compute
label intent, run the outbound differ, and convert each ``OutboundMutation`` into a
typed ``Mutation`` appended onto the shared mutation list. Its two fail-open alert
emitters (``_emit_outbound_field_alerts`` / ``_emit_recovery_failure_alerts``)
deliberately stayed in ``run_differs.py`` rather than moving alongside it: several
tests call them directly against a by-file-path-loaded copy of ``run_differs.py`` and
patch THAT module's own ``_load`` helper to stub the alert store, a patch that can
only take effect on a function whose ``__globals__`` is that same module's dict.
``_run_differs_outbound`` below calls them back via a local import (mirroring the
``refresh_scoped_snapshot`` local-import convention in ``run_differs.py``) so the
call always resolves against the real package's ``run_differs`` module.

This is a pure relocation: no behavior, call order, or payload shape changed. See
``run_differs.py`` for the phases that run before/after this one within a pass
(invariants, the legacy snapshot diff, the inbound differ, the binding walk).

Loader convention: like every sibling in this package, this module loads its own
siblings (``mutation.py``) by file path via the local ``_load`` helper
(``importlib.util.spec_from_file_location``), so it resolves both under the real
package and when loaded standalone in tests. Its only back-edge to ``run_differs.py``
is the local import inside ``_run_differs_outbound`` described above; ``ctx`` is typed
loosely to avoid importing ``_PassContext``.
"""

from __future__ import annotations

import importlib.util
import sys
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
        _loader_spec.loader.exec_module(_loader_mod)
    lazy_load = sys.modules[_loader_key].lazy_load

# ADR 0107 "Cut" step: producers construct native typed payload dataclasses
# directly instead of raw dicts. mutation_payloads.py carries no cross-reload
# class-identity requirement (it never references MutationDirection/
# MutationAction), so an ordinary package import is safe here.
try:
    from rebar_reconciler.mutation_payloads import (
        OutboundCreatePayload,
        OutboundDeletePayload,
        OutboundUpdatePayload,
    )
except ImportError:  # standalone load without package context
    _mp_key = "rebar_reconciler.mutation_payloads"
    if _mp_key not in sys.modules:
        _mp_spec = importlib.util.spec_from_file_location(
            _mp_key, Path(__file__).parent / "mutation_payloads.py"
        )
        assert _mp_spec is not None and _mp_spec.loader is not None
        _mp_mod = importlib.util.module_from_spec(_mp_spec)
        sys.modules[_mp_key] = _mp_mod
        _mp_spec.loader.exec_module(_mp_mod)
    _mp_mod = sys.modules[_mp_key]
    OutboundCreatePayload = _mp_mod.OutboundCreatePayload
    OutboundUpdatePayload = _mp_mod.OutboundUpdatePayload
    OutboundDeletePayload = _mp_mod.OutboundDeletePayload


def _load(name: str, relpath: str):
    """Load a sibling module by relative file path, registering it in sys.modules.

    Returns the cached module when ``name`` is already in ``sys.modules``;
    this allows test fixtures to pre-register patched modules and have
    ``outbound_pass`` reuse them rather than loading fresh copies. Delegates to the
    shared ``lazy_load`` helper (the package-wide by-path loader, mirrored by
    run_differs.py and reconcile.py).
    """
    return lazy_load(name, relpath)


def _run_differs_outbound(ctx: Any, mutations, backend) -> tuple[list, dict, Any]:
    """Outbound differ phase: recover bindings, compute label intent + the outbound
    differ, and convert each OutboundMutation -> typed Mutation onto ``mutations``.

    ``backend`` is the configured :class:`Backend` (ticket 4af8); its ``.transport`` is
    the live-comment/recovery client and its ``.outbound`` mapper is injected into
    ``compute_outbound_mutations``.

    Returns ``(outbound_raw, absent_alive_fields, outbound_diff_client)`` for the
    inbound differ + binding-walk phases that follow.
    """
    # Recovery is a whole-store side effect, so any scoped pass suppresses it.
    # This is separate from the legacy post-differ write filter in reconcile.py.
    scoped_ids = ctx.filter_local_ids or ctx.selection_ids
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
    # S4: obtain the outbound-diff transport from the configured backend (routes
    # through the Backend port instead of constructing an AcliClient inline). The
    # backend is resolved once by the orchestrator and threaded in (ticket 4af8).
    outbound_diff_client = backend.transport

    # Filtered passes skip pending-binding recovery to avoid finalizing
    # bindings for non-test tickets (scope leak). A no-write pass (preview /
    # legacy --mode dry-run) also skips it: recovery's keyed-pending branch issues
    # REMOTE add_label / set_entity_property writes, which a documented no-write pass
    # must not perform (bug 7851). This mirrors the sibling invariant-filing gate
    # (skip_invariant_filing = (not persist) or bool(scoped_ids)) and the
    # interrupted-retirement write-boundary gate composed in reconcile.py. The
    # scoped check is first so a scoped pass never depends on the persist axis.
    ctx.recovery_failures = 0
    if not scoped_ids and ctx.persist:
        recovery_failures: list[dict[str, Any]] = []
        try:
            binding_store.recover_pending_bindings(
                outbound_diff_client, failure_sink=recovery_failures
            )
        except Exception as exc:  # noqa: BLE001 — fail-open: a total recovery failure is non-fatal
            recovery_failures.append({"local_id": "<all>", "reason": repr(exc)})
            print(
                f"reconcile: binding recovery failed ({exc}), continuing",
                file=sys.stderr,
            )
        # LOUD (story 9622): surface per-entry failures instead of the silent
        # swallow — a deduped bridge alert each + a nonzero recovery_failures tally.
        if recovery_failures:
            ctx.recovery_failures = len(recovery_failures)
            # Local import: ``_emit_recovery_failure_alerts`` stayed in run_differs.py
            # (see module docstring) — resolve it against THAT module's namespace so
            # a monkeypatch/patch.object on run_differs still intercepts it.
            from rebar_reconciler.run_differs import _emit_recovery_failure_alerts

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
    # Story d19d: hand the differ the store's projects mapping so the create path
    # can resolve each ticket's target project. An ABSENT projects.json loads as an
    # empty Mapping (legacy single-project behaviour); a MALFORMED one fails closed.
    from rebar_reconciler import projects_store as _projects_store_mod

    _projects_mapping = _projects_store_mod.load_mapping(repo_root)
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
            projects_mapping=_projects_mapping,
            repo_root=repo_root,
        ),
        outbound_mapper=backend.outbound,
        inbound_mapper=backend.inbound,
        links=backend,
    )
    # Local import: ``_emit_outbound_field_alerts`` stayed in run_differs.py (see
    # module docstring) — resolve it against THAT module's namespace so a
    # monkeypatch/patch.object on run_differs still intercepts it.
    from rebar_reconciler.run_differs import _emit_outbound_field_alerts

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
    print(
        f"RECON: outbound_differ total={len(outbound_raw)} "
        f"create={_ob_creates} update={_ob_updates} delete={_ob_deletes}",
        file=sys.stderr,
    )

    # Convert OutboundMutation → typed Mutation for unified dispatch. ADR 0107 "Cut"
    # step: the payload itself is now one of the ten typed dataclasses
    # (mutation_payloads.py), constructed directly by this producer — not a raw dict
    # later disambiguated at dispatch time by ``batch_dispatch._mutation_to_batch_dict``.
    for om in outbound_raw:
        if om.action == "create":
            typed = mut_mod.Mutation(
                direction=mut_mod.MutationDirection.outbound,
                action=mut_mod.MutationAction.create,
                target=om.local_id,
                payload=OutboundCreatePayload(
                    fields=om.fields,
                    comments=tuple(om.comments),
                    labels=tuple(om.labels),
                    local_id=om.local_id,
                ),
                provenance={"source": "outbound_differ", "local_id": om.local_id},
            )
        elif om.action == "update":
            typed = mut_mod.Mutation(
                direction=mut_mod.MutationDirection.outbound,
                action=mut_mod.MutationAction.update,
                target=om.jira_key or om.local_id,
                payload=OutboundUpdatePayload(
                    changed_fields=om.fields,
                    comments=tuple(om.comments),
                    labels=tuple(om.labels),
                    # Cycle 3: link adds ride the existing update payload
                    # (no new MutationAction) — _apply_outbound_update reads
                    # payload["links"] and calls client.set_relationship.
                    links=tuple(getattr(om, "links", [])),
                ),
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
                payload=OutboundDeletePayload(),
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
