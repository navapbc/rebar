"""Production-flip regression for single-vast-roan / ADR 0107's "Cut" step.

Before this story, ``outbound_pass._run_differs_outbound`` converted each
``OutboundMutation`` into a typed ``Mutation`` whose ``.payload`` was a raw
dict (top-level-spread for create, ``changed_fields``/``comments``/``labels``
keys for update, ``{}`` for delete) — the ambiguous shape
``batch_dispatch._mutation_to_batch_dict`` had to runtime-sniff. This test
pins the POST-cutover behavior: the producer now constructs one of the typed
``mutation_payloads`` dataclasses directly, so the payload shape is
structurally unambiguous before it ever reaches dispatch.

Confirmed RED against the pre-cutover code (payload was a plain ``dict``, so
every ``isinstance(..., OutboundXPayload)`` assertion below failed).
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def run_differs_outbound():
    from rebar_reconciler import local_label_intent, outbound_differ, run_differs

    return run_differs._run_differs_outbound, outbound_differ, local_label_intent


def _make_ctx(tmp_path: Path, outbound_differ_mod, local_label_intent_mod) -> Any:
    return types.SimpleNamespace(
        filter_local_ids=["scope"],  # scoped pass: skip binding recovery (no transport calls)
        selection_ids=None,
        binding_store=types.SimpleNamespace(get_jira_key=lambda _id: None),
        local_tickets=[],
        local_label_intent_mod=local_label_intent_mod,
        tracker_dir=tmp_path / ".tickets-tracker",
        repo_root=tmp_path,
        outbound_differ_mod=outbound_differ_mod,
        pass_id="p-typed-cutover",
        prev_snapshot={},
        curr_snapshot={},
        sync_logger=types.SimpleNamespace(log=lambda *a, **k: None),
        recovery_failures=0,
    )


def test_outbound_create_mutation_carries_a_typed_payload(
    run_differs_outbound, tmp_path, monkeypatch
):
    run_fn, outbound_differ_mod, local_label_intent_mod = run_differs_outbound
    from rebar_reconciler.mutation_payloads import OutboundCreatePayload

    create_om = outbound_differ_mod.OutboundMutation(
        local_id="local-1",
        jira_key=None,
        action="create",
        fields={"summary": "New issue", "issuetype": "Task"},
        comments=[{"body": "hello"}],
        labels=[{"action": "add", "label": "x"}],
    )
    monkeypatch.setattr(
        outbound_differ_mod,
        "compute_outbound_mutations",
        lambda *a, **k: ([create_om], {}),
    )
    ctx = _make_ctx(tmp_path, outbound_differ_mod, local_label_intent_mod)
    backend = types.SimpleNamespace(transport=object(), outbound=object(), inbound=object())
    mutations: list = []

    run_fn(ctx, mutations, backend)

    assert len(mutations) == 1
    payload = mutations[0].payload
    assert isinstance(payload, OutboundCreatePayload), (
        f"expected outbound_pass to construct an OutboundCreatePayload, got {type(payload)!r} "
        f"({payload!r}) — production must no longer build a raw dict payload for outbound "
        "creates (ADR 0107 'Cut' step)."
    )
    assert payload.fields == {"summary": "New issue", "issuetype": "Task"}
    assert payload.comments == ({"body": "hello"},)
    assert payload.labels == ({"action": "add", "label": "x"},)
    assert payload.local_id == "local-1"


def test_outbound_update_mutation_carries_a_typed_payload(
    run_differs_outbound, tmp_path, monkeypatch
):
    run_fn, outbound_differ_mod, local_label_intent_mod = run_differs_outbound
    from rebar_reconciler.mutation_payloads import OutboundUpdatePayload

    update_om = outbound_differ_mod.OutboundMutation(
        local_id="local-2",
        jira_key="DIG-100",
        action="update",
        fields={"summary": "changed"},
        comments=[],
        labels=[{"action": "remove", "label": "y"}],
        links=[{"action": "add", "type": "blocks", "key": "DIG-1"}],
    )
    monkeypatch.setattr(
        outbound_differ_mod,
        "compute_outbound_mutations",
        lambda *a, **k: ([update_om], {}),
    )
    ctx = _make_ctx(tmp_path, outbound_differ_mod, local_label_intent_mod)
    backend = types.SimpleNamespace(transport=object(), outbound=object(), inbound=object())
    mutations: list = []

    run_fn(ctx, mutations, backend)

    assert len(mutations) == 1
    payload = mutations[0].payload
    assert isinstance(payload, OutboundUpdatePayload), (
        f"expected outbound_pass to construct an OutboundUpdatePayload, got {type(payload)!r} "
        f"({payload!r}) — production must no longer build a raw dict payload for outbound "
        "updates (ADR 0107 'Cut' step)."
    )
    assert payload.changed_fields == {"summary": "changed"}
    assert payload.labels == ({"action": "remove", "label": "y"},)
    assert payload.links == ({"action": "add", "type": "blocks", "key": "DIG-1"},)


def test_outbound_delete_mutation_carries_a_typed_payload(
    run_differs_outbound, tmp_path, monkeypatch
):
    run_fn, outbound_differ_mod, local_label_intent_mod = run_differs_outbound
    from rebar_reconciler.mutation_payloads import OutboundDeletePayload

    delete_om = outbound_differ_mod.OutboundMutation(
        local_id="local-3",
        jira_key="DIG-101",
        action="delete",
        fields={},
    )
    monkeypatch.setattr(
        outbound_differ_mod,
        "compute_outbound_mutations",
        lambda *a, **k: ([delete_om], {}),
    )
    ctx = _make_ctx(tmp_path, outbound_differ_mod, local_label_intent_mod)
    backend = types.SimpleNamespace(transport=object(), outbound=object(), inbound=object())
    mutations: list = []

    run_fn(ctx, mutations, backend)

    assert len(mutations) == 1
    assert isinstance(mutations[0].payload, OutboundDeletePayload)


def test_typed_create_payload_round_trips_through_mutation_to_batch_dict(
    run_differs_outbound, tmp_path, monkeypatch
):
    """End-to-end: the typed payload the producer now builds must still
    dispatch correctly through batch_dispatch._mutation_to_batch_dict —
    i.e. the "Cut" (producer) and simplified "Delete" (dispatch ambiguity
    removal) halves of this story compose correctly."""
    run_fn, outbound_differ_mod, local_label_intent_mod = run_differs_outbound
    from rebar_reconciler import batch_dispatch

    create_om = outbound_differ_mod.OutboundMutation(
        local_id="local-4",
        jira_key=None,
        action="create",
        fields={"summary": "New issue", "fields": "not-a-bookkeeping-collision"},
        comments=[],
        labels=[],
    )
    monkeypatch.setattr(
        outbound_differ_mod,
        "compute_outbound_mutations",
        lambda *a, **k: ([create_om], {}),
    )
    ctx = _make_ctx(tmp_path, outbound_differ_mod, local_label_intent_mod)
    backend = types.SimpleNamespace(transport=object(), outbound=object(), inbound=object())
    mutations: list = []
    run_fn(ctx, mutations, backend)

    batch_dict = batch_dispatch._mutation_to_batch_dict(mutations[0])

    # The old dict-payload heuristic would have been fooled by a literal
    # "fields" key living INSIDE the create fields themselves (the very
    # ambiguity this story's typed cutover eliminates for the production
    # path) — the typed payload's own `.fields` attribute is unambiguous.
    assert batch_dict["fields"] == {
        "summary": "New issue",
        "fields": "not-a-bookkeeping-collision",
    }
