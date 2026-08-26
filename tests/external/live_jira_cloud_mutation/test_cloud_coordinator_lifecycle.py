"""Bounded, self-cleaning live-Jira-Cloud probe of the RP-03 coordinator write paths.

The epic's Live-External AC requires the create-coordinator's create / binding-lifecycle /
commit-unknown / fuse to be proven against REAL Jira Cloud (the read-only S3 rehearsal
cannot). Each test here drives ONE of those seams against live Cloud through the SAME public
facade production uses — ``run_coordinated_outbound_create`` for the create+containment
composition, ``coordinate_and_fuse`` for the non-create fuse pass — never a bespoke
re-implementation, so a green run proves the shipped wiring, not a test double.

SELF-CLEANING CONTRACT. Every issue is created with a UNIQUE ``rebar-id:<local_id>`` binding
label, immediately stamped with the run-scoped ``REBAR_PROBE_RUN_LABEL``, and DELETED by key
in a ``finally``. The conftest label-sweep and the workflow's always-run acli teardown are
crash backstops. Nothing is enumerated and no ``--filter-local-ids`` legacy path is used: the
coordinator is handed exactly ONE plan, structurally bounding the blast radius to one issue.

Gating: the parent ``tests/external/conftest.py`` skips the whole tier unless
``REBAR_RUN_EXTERNAL`` is set; ``live_jira_ready()`` skips when creds/acli are absent; and the
module-level ``_live_jira_ready`` sentinel (in the conftest) enrols the suite in the all-skip
canary so a fully-skipped run FAILS instead of reporting green.
"""

from __future__ import annotations

import os

from _cloud_mutation_support import (
    engine_on_path,
    new_local_id,
    run_label,
    wait_visible,
)

engine_on_path()

from rebar_reconciler.batch_dispatch import coordinate_and_fuse  # noqa: E402
from rebar_reconciler.coordinator import AtomicSignal  # noqa: E402
from rebar_reconciler.create_route import run_coordinated_outbound_create  # noqa: E402
from rebar_reconciler.mutation import (  # noqa: E402
    Mutation,
    MutationAction,
    MutationDirection,
)
from rebar_reconciler.ticket_plan import PlanDisposition, TicketPlan  # noqa: E402

# Sentinel re-exported so this module ALSO carries it (the conftest defines the canonical
# one; a test module may be collected on its own path, so keep it discoverable here too).
_live_jira_ready = True


def _stamp_run_label(client, key: str) -> None:
    """Add the run-scoped sweep label so a crash backstop can find this issue."""
    client.add_label(key, run_label())


def test_create_and_binding_lifecycle_against_live_cloud(cloud_client) -> None:
    """Create + record-key + label + property + confirm compose to a released binding.

    Drives the real ``run_coordinated_outbound_create`` facade against live Cloud with the
    binding store elided (``binding_store=None``), so the create, the canonical
    ``rebar-id:<local_id>`` label, and the ``local_id`` entity property all land on a real
    issue and the composition confirms — releasing dependents (AC5). The created issue is
    then re-observed via JQL under the widened index-lag backoff to prove the binding is
    durably searchable, and deleted in ``finally``.
    """
    local_id = new_local_id()
    payload = {
        "local_id": local_id,
        "title": f"[rebar cloud-mutation probe] create/bind {local_id}",
        "ticket_type": "task",
        "description": "Throwaway issue for the RP-03 live-Cloud coordinator probe.",
    }
    mutation = Mutation(
        direction=MutationDirection.outbound,
        action=MutationAction.create,
        target=local_id,
        payload=payload,
        provenance={"src": "live-cloud-mutation-probe"},
    )

    known_key: str | None = None
    try:
        outcome = run_coordinated_outbound_create(mutation, client=cloud_client, binding_store=None)
        known_key = outcome.known_key
        if known_key:
            _stamp_run_label(cloud_client, known_key)

        assert known_key, f"create did not land a key against live Cloud; outcome={outcome!r}"
        assert outcome.confirmed, (
            f"the create+containment composition did not confirm; outcome={outcome!r}"
        )
        assert outcome.label_attached, "the canonical rebar-id label was not attached"
        assert outcome.property_attached, "the local_id entity property was not set"
        assert outcome.dependents_released, "a confirmed containment must release dependents (AC5)"

        # The binding must be durably searchable — the exact re-observe the coordinator
        # itself relies on, hardened by the §1 capped-exponential backoff for index lag.
        hits = wait_visible(cloud_client, f'labels = "rebar-id:{local_id}"')
        keys = {h.get("key") for h in hits}
        assert known_key in keys, (
            f"created issue {known_key} was not searchable by its rebar-id label; got {keys}"
        )
    finally:
        if known_key:
            cloud_client.delete_issue(known_key)


def test_fuse_pass_applies_a_bounded_mutation_against_live_cloud(cloud_client) -> None:
    """A single-plan non-create pass drives ``coordinate_and_fuse`` to an applied tally.

    Provisions one throwaway issue, then hands the coordinator exactly ONE update plan whose
    ``execute`` adapter performs the live Jira update and reports ``applied``. The returned
    :class:`CutoverReport` must tally one ``applied`` outcome, raise no fuse decision, and be
    non-degraded — proving the real fuse fold over a live outcome. The issue is deleted in
    ``finally``.
    """
    local_id = new_local_id()
    created = cloud_client.create_issue(
        {
            "local_id": local_id,
            "title": f"[rebar cloud-mutation probe] fuse {local_id}",
            "ticket_type": "task",
        }
    )
    key = created.get("key")
    assert key, f"could not provision a throwaway issue for the fuse probe: {created!r}"

    try:
        cloud_client.add_label(key, f"rebar-id:{local_id}")
        _stamp_run_label(cloud_client, key)

        update = Mutation(
            direction=MutationDirection.outbound,
            action=MutationAction.update,
            target=key,
            payload={"summary": f"[rebar cloud-mutation probe] fused {local_id}"},
            provenance={"src": "live-cloud-mutation-probe"},
        )
        plan = TicketPlan(
            identity=key,
            mutations=(update,),
            diagnostics=(),
            disposition=PlanDisposition("mutate"),
            observation_version="live-cloud",
            payload={},
            dependencies=(),
            defer_reason=None,
        )

        applied: list[str] = []

        def live_execute(ticket_plan, mut) -> AtomicSignal:
            cloud_client.update_issue(ticket_plan.identity, summary=mut.payload["summary"])
            applied.append(ticket_plan.identity)
            return AtomicSignal(status="applied")

        endpoint = os.environ["JIRA_URL"]
        report = coordinate_and_fuse(
            [plan],
            execute=live_execute,
            locate=lambda identity: {"provider": "jira", "endpoint": endpoint},
        )

        assert applied == [key], f"the live execute adapter was not driven once: {applied}"
        assert report.degraded is False, f"a clean single-plan pass must not degrade: {report!r}"
        assert report.tallies.get("applied") == 1, (
            f"one applied mutation must tally as one 'applied'; tallies={dict(report.tallies)}"
        )
        assert report.tallies.get("failed", 0) == 0, (
            f"no mutation failed, so 'failed' must be 0; tallies={dict(report.tallies)}"
        )
        assert not report.fuse_decisions, (
            f"a clean pass must raise no fuse decision; got {report.fuse_decisions!r}"
        )
    finally:
        cloud_client.delete_issue(key)
