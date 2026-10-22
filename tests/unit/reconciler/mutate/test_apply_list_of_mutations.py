"""Keep typed mutation batches JSON-safe and fail closed (bug 1788-6149-e788-463f).

Outbound ``Mutation`` objects reach ``_apply_batch`` as dictionaries containing its
expected action, fields, key, local-id, follow-on, and direction values. Empty fields
remain empty. Inbound typed mutations raise ``TypeError`` instead of entering outbound
handlers. Since ADR 0103 S3 the batch form also requires ``ticket_plans``, and a dict no
plan routes is recorded failed rather than dispatched.
"""

import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rebar.reconciler import ticket_planner
from rebar.reconciler.mutation_payloads import OutboundCreatePayload, OutboundUpdatePayload

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "applier.py"
MUTATION_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "mutation.py"


def _plans(mutations, *, mode=None, max_changes=None):
    """Ticket plans for the outbound ``mutations``, built as ``run_differs`` builds them.

    The batch form of ``applier.apply`` requires ``ticket_plans`` (ADR 0103 S3). Inbound
    mutations dispatch through ``_apply_typed``, so only the outbound ones are planned.
    """
    _, plans = ticket_planner.plan_pass(
        pass_id="test-pass",
        local_snapshot={},
        remote_snapshot={},
        binding_view={},
        mode=str(getattr(mode, "value", mode) or ""),
        selection={"kind": None, "ids": []},
        limits={"max_changes": max_changes},
        mutations=[
            m
            for m in mutations
            if getattr(getattr(m, "direction", None), "value", None) == "outbound"
        ],
    )
    return plans


@pytest.fixture
def applier_and_mutation():
    """The shared ``rebar.reconciler`` applier and mutation modules."""
    return (
        importlib.import_module("rebar.reconciler.applier"),
        importlib.import_module("rebar.reconciler.mutation"),
    )


def _make_outbound_create(mut_mod, target="DIG-100", payload=None):
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.outbound,
        action=mut_mod.MutationAction.create,
        target=target,
        payload=payload or OutboundCreatePayload(fields={"summary": "test"}, local_id="local-abc"),
        provenance={"source": "test"},
    )


def test_list_of_mutation_normalized_to_dict_before_apply_batch(applier_and_mutation, tmp_path):
    """applier.apply([Mutation], ...) must pass list-of-dict (not list-of-
    Mutation) to _apply_batch. Assert observable behavior: _apply_batch
    is called with dicts having the expected keys; no Mutation leaks.
    """
    applier, mut_mod = applier_and_mutation
    mutations = [_make_outbound_create(mut_mod)]

    with patch.object(applier, "_apply_batch", return_value=tmp_path / "m.json") as ab:
        applier.apply(mutations, "pass-1", repo_root=tmp_path, ticket_plans=_plans(mutations))

    assert ab.call_count == 1
    passed_list = ab.call_args[0][0]
    assert isinstance(passed_list, list)
    assert all(isinstance(m, dict) for m in passed_list), (
        f"Mutation leaked through: {[type(m).__name__ for m in passed_list]}"
    )
    assert all("action" in m and "key" in m for m in passed_list), (
        "normalized dicts missing required batch keys"
    )


def test_normalized_dict_is_json_serializable(applier_and_mutation, tmp_path):
    """Every value in the normalized dict must be JSON-serializable —
    _apply_batch later writes the manifest via json.dumps. A non-
    serializable value (e.g. a Mutation back-reference) would crash there.
    """
    applier, mut_mod = applier_and_mutation
    mutations = [_make_outbound_create(mut_mod)]

    with patch.object(applier, "_apply_batch", return_value=tmp_path / "m.json") as ab:
        applier.apply(mutations, "pass-2", repo_root=tmp_path, ticket_plans=_plans(mutations))

    passed_list = ab.call_args[0][0]
    for m in passed_list:
        try:
            json.dumps(m)
        except TypeError as exc:
            pytest.fail(
                f"normalized dict is not JSON-serializable ({exc}); "
                f"a non-serializable value (e.g. Mutation back-ref) would "
                f"crash _apply_batch's manifest write"
            )


def test_empty_fields_does_not_fall_through_to_full_payload(applier_and_mutation, tmp_path):
    """An intentionally empty create field map must reach _apply_batch as {} (not as
    the full payload), so local_id never leaks into the batch fields."""
    applier, mut_mod = applier_and_mutation
    mutations = [
        _make_outbound_create(mut_mod, payload=OutboundCreatePayload(fields={}, local_id="L1")),
    ]

    with patch.object(applier, "_apply_batch", return_value=tmp_path / "m.json") as ab:
        applier.apply(mutations, "pass-3", repo_root=tmp_path, ticket_plans=_plans(mutations))

    passed_list = ab.call_args[0][0]
    assert passed_list[0]["fields"] == {}, (
        f"empty fields fell through to full payload: {passed_list[0]['fields']!r}"
    )
    # local_id is still preserved as a top-level key
    assert passed_list[0]["local_id"] == "L1"


def test_plain_dict_create_payload_raises_type_error(applier_and_mutation, tmp_path):
    """A typed outbound create whose payload is a plain dict has no single unambiguous
    field map, so the batch form refuses it before any dispatch — inbound included."""
    applier, mut_mod = applier_and_mutation
    mutations = [
        _make_inbound_create(mut_mod, target="local-first"),
        _make_outbound_create(mut_mod, payload={"fields": {"summary": "x"}}),
    ]

    with (
        patch.object(applier, "_apply_typed") as at,
        patch.object(applier, "_apply_batch", return_value=tmp_path / "m.json") as ab,
    ):
        with pytest.raises(TypeError, match="OutboundCreatePayload"):
            applier.apply(
                mutations,
                "pass-plain-create",
                repo_root=tmp_path,
                client=object(),
                ticket_plans=_plans(mutations),
            )
    assert at.call_count == 0
    assert ab.call_count == 0


def test_untyped_batch_element_raises_type_error(applier_and_mutation, tmp_path):
    """The batch form takes typed Mutations only: a raw dict element raises TypeError
    before any dispatch, transport load, or write."""
    applier, _mut_mod = applier_and_mutation
    fake_client = object()

    with (
        patch.object(applier, "_apply_batch") as ab,
        patch.object(applier, "_apply_typed") as at,
    ):
        with pytest.raises(TypeError, match="typed Mutations only"):
            applier.apply(
                [{"action": "update"}],
                "p",
                tmp_path,
                client=fake_client,
                ticket_plans=_plans([{"action": "update"}]),
            )
    assert ab.call_count == 0
    assert at.call_count == 0


def test_batch_form_without_ticket_plans_raises_type_error(applier_and_mutation, tmp_path):
    """ADR 0103 S3: the batch form requires ``ticket_plans`` — there is no legacy
    per-mutation loop left to route a plan-less batch — so omitting them raises
    ``TypeError`` before any dispatch, transport load, or write. An empty batch too."""
    applier, mut_mod = applier_and_mutation

    with (
        patch.object(applier, "_apply_batch") as ab,
        patch.object(applier, "_apply_typed") as at,
    ):
        with pytest.raises(TypeError, match="requires ticket_plans"):
            applier.apply([_make_outbound_create(mut_mod)], "p", tmp_path, client=object())
        with pytest.raises(TypeError, match="requires ticket_plans"):
            applier.apply([], "p", tmp_path, client=object())
    assert ab.call_count == 0
    assert at.call_count == 0


def test_mixed_create_and_noncreate_plan_records_dicts_failed_not_dispatched(
    applier_and_mutation, tmp_path
):
    """ADR 0103 S3: a plan mixing a create with a non-create action is routed by
    neither the create reroute nor the coordinator reroute. Its dicts are recorded as
    failed outcomes (``unrouted mutation``) without a vendor call — never dropped, and
    never dispatched through a retired legacy loop."""
    applier, mut_mod = applier_and_mutation
    create = _make_outbound_create(mut_mod, target="DIG-77")
    update = mut_mod.Mutation(
        direction=mut_mod.MutationDirection.outbound,
        action=mut_mod.MutationAction.update,
        target="DIG-77",
        payload=OutboundUpdatePayload(changed_fields={"summary": "s"}, local_id="local-abc"),
        provenance={"source": "test"},
    )
    plans = _plans([create, update])
    assert [sorted(m.action.value for m in p.mutations) for p in plans] == [["create", "update"]]
    client = MagicMock()

    with patch.object(applier, "_load_acli", return_value=client):
        manifest_path = applier.apply(
            [create, update], "pass-mixed-plan", repo_root=tmp_path, ticket_plans=plans
        )

    outcomes = json.loads(Path(manifest_path).read_text())["mutations"]
    assert sorted(o["action"] for o in outcomes) == ["create", "update"]
    for outcome in outcomes:
        assert "unrouted mutation" in str(outcome.get("error")), outcome
    assert client.mock_calls == [], (
        f"an unrouted dict must not reach the vendor: {client.mock_calls}"
    )


def _two_independent_creates(mut_mod):
    return [
        _make_outbound_create(
            mut_mod,
            target=f"local-{n}",
            payload=OutboundCreatePayload(fields={"summary": n}, local_id=f"local-{n}"),
        )
        for n in ("one", "two")
    ]


def test_lost_lease_stops_the_create_reroute_before_any_vendor_call(applier_and_mutation, tmp_path):
    """The create reroute runs the per-mutation lost-lease checkpoint before each create
    it dispatches (the retired legacy loop ran it before every dispatch), so a lost lease
    aborts the pass before the first create reaches the vendor."""
    applier, mut_mod = applier_and_mutation
    creates = _two_independent_creates(mut_mod)
    plans = _plans(creates)
    assert sorted(m.action.value for p in plans for m in p.mutations) == ["create", "create"]
    client = MagicMock()

    class _LeaseLost(Exception):
        pass

    def abort_check():
        raise _LeaseLost("lease-lost")

    with patch.object(applier, "_load_acli", return_value=client):
        with pytest.raises(_LeaseLost):
            applier.apply(
                creates, "pass-lease-lost", tmp_path, ticket_plans=plans, abort_check=abort_check
            )
    assert not client.create_issue.called


def test_create_reroute_gates_every_create_dispatch(applier_and_mutation, tmp_path):
    """Each create dispatch is preceded by the lost-lease checkpoint and the HEAD
    re-check, in that order, once per create."""
    applier, mut_mod = applier_and_mutation
    from rebar.reconciler import batch_dispatch
    from rebar.reconciler.apply_handlers import HandlerResult

    creates = _two_independent_creates(mut_mod)
    plans = _plans(creates)
    events: list[str] = []
    real_recheck = applier._recheck_drift

    def recheck(snapshot_head, repo_root, pin):
        events.append("recheck")
        return real_recheck(snapshot_head, repo_root, pin)

    def dispatch(batch, ctx):
        events.append(f"dispatch:{batch['key']}")
        return HandlerResult({**batch, "result": {"key": batch["key"]}, "error": None})

    with (
        patch.object(applier, "_load_acli", return_value=MagicMock()),
        patch.object(applier, "_recheck_drift", side_effect=recheck),
        patch.object(batch_dispatch, "_dispatch_with_backstop", side_effect=dispatch),
    ):
        applier.apply(
            creates,
            "pass-gated-creates",
            tmp_path,
            ticket_plans=plans,
            abort_check=lambda: events.append("abort_check"),
        )
    assert events == [
        "abort_check",
        "recheck",
        "dispatch:local-one",
        "abort_check",
        "recheck",
        "dispatch:local-two",
    ]


def test_typed_update_with_plain_dict_changed_fields_converts(applier_and_mutation):
    """The binding_walk shape — a typed outbound update whose payload is a plain dict
    carrying ``changed_fields`` — still converts to a batch dict."""
    applier, mut_mod = applier_and_mutation
    mutation = mut_mod.Mutation(
        direction=mut_mod.MutationDirection.outbound,
        action=mut_mod.MutationAction.update,
        target="DIG-7",
        payload={
            "changed_fields": {"status": "Done"},
            "comments": [],
            "labels": [],
            "links": [],
        },
        provenance={"source": "binding_walk"},
    )

    batch = applier._mutation_to_batch_dict(mutation)

    assert batch["action"] == "update"
    assert batch["key"] == "DIG-7"
    assert batch["fields"] == {"status": "Done"}
    assert batch["comments"] == [] and batch["labels"] == [] and batch["links"] == []


def _make_inbound_create(mut_mod, target="local-abc"):
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target=target,
        payload={"fields": {"summary": "in"}, "local_id": target},
        provenance={"source": "test"},
    )


def test_inbound_mutations_dispatched_per_mutation_via_apply_typed(applier_and_mutation, tmp_path):
    """`apply(list[Mutation], pass_id, repo_root)` with inbound Mutations
    routes each one through `_apply_typed` (per-mutation dispatch via the
    _LEAVES registry) rather than the outbound batch path. Defect #8 — the
    previous fail-closed guard correctly identified this gap; this test pins
    the actual routing.
    """
    applier, mut_mod = applier_and_mutation
    inbound = [_make_inbound_create(mut_mod, target=f"local-{i}") for i in range(3)]

    with (
        patch.object(applier, "_apply_typed") as at,
        patch.object(applier, "_apply_batch", return_value=tmp_path / "m.json") as ab,
    ):
        applier.apply(inbound, "pass-inbound", repo_root=tmp_path, ticket_plans=_plans(inbound))

    # Each inbound Mutation reached _apply_typed exactly once, in order.
    assert at.call_count == len(inbound), (
        f"Expected {len(inbound)} per-mutation _apply_typed dispatches; got {at.call_count}"
    )
    dispatched_targets = [c.args[0].target for c in at.call_args_list]
    assert dispatched_targets == [m.target for m in inbound]
    # No inbound mutation leaked into the outbound batch path — _apply_batch
    # was called with an empty list (or not called at all if the impl skips
    # the empty-batch invocation; both are acceptable as long as no inbound
    # Mutation reaches it).
    if ab.call_count > 0:
        passed = ab.call_args[0][0]
        assert all(
            not (
                hasattr(m, "direction")
                and str(getattr(m.direction, "value", m.direction)) == "inbound"
            )
            for m in passed
        ), f"Inbound Mutation leaked into _apply_batch: {passed!r}"


def test_mixed_inbound_and_outbound_partitioned_correctly(applier_and_mutation, tmp_path):
    """When `mutations` mixes inbound + outbound, the inbound ones go
    through `_apply_typed` (per-mutation) and the outbound ones go through
    `_apply_batch` (normalized to dicts). No mutation crosses paths."""
    applier, mut_mod = applier_and_mutation
    mutations = [
        _make_outbound_create(mut_mod, target="DIG-1"),
        _make_inbound_create(mut_mod, target="local-1"),
        _make_outbound_create(mut_mod, target="DIG-2"),
        _make_inbound_create(mut_mod, target="local-2"),
    ]

    with (
        patch.object(applier, "_apply_typed") as at,
        patch.object(applier, "_apply_batch", return_value=tmp_path / "m.json") as ab,
    ):
        applier.apply(mutations, "pass-mixed", repo_root=tmp_path, ticket_plans=_plans(mutations))

    # Inbound: per-mutation dispatch via _apply_typed (2 of them).
    assert at.call_count == 2
    dispatched_inbound = [c.args[0].target for c in at.call_args_list]
    assert set(dispatched_inbound) == {"local-1", "local-2"}

    # Outbound: batch dispatch via _apply_batch (1 call with 2 dicts).
    assert ab.call_count == 1
    batch_arg = ab.call_args[0][0]
    assert len(batch_arg) == 2, f"Expected 2 outbound dicts; got {len(batch_arg)}: {batch_arg!r}"
    assert all(isinstance(m, dict) for m in batch_arg), (
        f"Outbound items were not normalized to dicts: {[type(m).__name__ for m in batch_arg]}"
    )
    batch_keys = {m["key"] for m in batch_arg}
    assert batch_keys == {"DIG-1", "DIG-2"}


def test_all_inbound_does_not_crash_when_outbound_batch_is_empty(applier_and_mutation, tmp_path):
    """When `mutations` is entirely inbound, the outbound batch path
    receives an empty list (or is skipped). Either way, `apply()` does
    not raise — defect #8 production scenario (empty local mirror,
    every Jira issue is an inbound 'create-locally')."""
    applier, mut_mod = applier_and_mutation
    inbound = [_make_inbound_create(mut_mod, target=f"local-{i}") for i in range(5)]

    with (
        patch.object(applier, "_apply_typed") as at,
        patch.object(applier, "_apply_batch", return_value=tmp_path / "m.json"),
    ):
        # Should NOT raise.
        applier.apply(inbound, "pass-all-inbound", repo_root=tmp_path, ticket_plans=_plans(inbound))

    assert at.call_count == 5
