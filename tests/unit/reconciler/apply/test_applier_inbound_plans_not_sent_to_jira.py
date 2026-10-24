"""Inbound ticket plans must never be dispatched through the outbound handlers.

``run_differs`` plans every typed mutation, inbound and outbound. ``applier.apply`` applies
inbound ones locally through ``_apply_typed`` and only the outbound ones through the batch
path, so the coordinator reroute must skip plans that carry an inbound mutation. Otherwise a
change read FROM Jira is written straight back TO Jira.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

from rebar.reconciler import ticket_planner
from rebar.reconciler.mutation import Mutation, MutationAction, MutationDirection
from rebar.reconciler.mutation_payloads import InboundUpdatePayload, OutboundUpdatePayload

_STABLE_HEAD = "abc1234def567890" * 2


def _inbound_update(jira_key: str, fields: dict, comments: tuple = ()) -> Mutation:
    return Mutation(
        MutationDirection.inbound,
        MutationAction.update,
        jira_key,
        InboundUpdatePayload(fields=fields, comments=comments),
        {},
    )


def _outbound_update(jira_key: str, fields: dict) -> Mutation:
    return Mutation(
        MutationDirection.outbound,
        MutationAction.update,
        jira_key,
        OutboundUpdatePayload(changed_fields=fields),
        {},
    )


def _production_plans(mutations):
    """Plans built exactly as ``run_differs`` builds them: every typed mutation, both directions."""
    _, plans = ticket_planner.plan_pass(
        pass_id="test-pass",
        local_snapshot={},
        remote_snapshot={},
        binding_view={},
        mode="",
        selection={"kind": None, "ids": []},
        limits={"max_changes": None},
        mutations=list(mutations),
    )
    return plans


def _apply(mutations, tmp_path):
    applier = importlib.import_module("rebar.reconciler.applier")
    client = MagicMock()
    client.update_issue.return_value = {"key": "DIG-2"}
    client.search_issues.return_value = []
    with (
        patch.object(applier, "_load_acli", return_value=client),
        patch.object(applier, "snapshot_head", return_value=_STABLE_HEAD),
        patch.object(applier, "_apply_typed"),
    ):
        applier.apply(
            mutations,
            pass_id="test-inbound-not-sent",
            repo_root=tmp_path,
            ticket_plans=_production_plans(mutations),
        )
    return client


def test_inbound_update_is_not_written_back_to_jira(tmp_path):
    """An inbound update beside an outbound one: only the outbound issue is written."""
    inbound = _inbound_update("DIG-1", {"summary": "edited in Jira"})
    outbound = _outbound_update("DIG-2", {"summary": "edited locally"})

    client = _apply([inbound, outbound], tmp_path)

    written = [c.args[0] for c in client.update_issue.call_args_list]
    assert written == ["DIG-2"], (
        f"only the outbound update may reach Jira; update_issue was called for {written}"
    )


def test_inbound_jira_comment_is_not_posted_back_to_jira(tmp_path):
    inbound = _inbound_update(
        "DIG-1",
        {"summary": "edited in Jira"},
        comments=({"body": "comment that came from Jira"},),
    )
    outbound = _outbound_update("DIG-2", {"summary": "edited locally"})

    client = _apply([inbound, outbound], tmp_path)

    assert client.add_comment.call_args_list == [], (
        "a comment read from Jira must not be posted to Jira again: "
        f"{client.add_comment.call_args_list}"
    )


def test_a_plan_mixing_directions_sends_only_its_outbound_half(tmp_path):
    """Plans group by target, so one issue can carry both directions. Its outbound update is
    still written; its inbound half (and the Jira comment it carries) is not."""
    inbound = _inbound_update(
        "DIG-1", {"summary": "edited in Jira"}, comments=({"body": "from Jira"},)
    )
    outbound = _outbound_update("DIG-1", {"summary": "edited locally"})

    client = _apply([inbound, outbound], tmp_path)

    assert [c.args[0] for c in client.update_issue.call_args_list] == ["DIG-1"]
    sent = client.update_issue.call_args_list[0]
    assert "edited in Jira" not in repr(sent), f"the inbound field value was sent: {sent}"
    assert client.add_comment.call_args_list == []


def test_inbound_only_batch_makes_no_jira_write(tmp_path):
    client = _apply([_inbound_update("DIG-1", {"summary": "edited in Jira"})], tmp_path)

    assert client.update_issue.call_args_list == []
    assert client.add_comment.call_args_list == []
