"""Verify a stale-binding 404 soft-fails within an update batch.

A missing Jira target records a failed mutation outcome, logs a warning, and
allows valid sibling updates to complete. Only HTTP 404 is softened. Other
HTTP errors retain their current behavior.
"""

from __future__ import annotations

import importlib
import json
import sys
import time
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from rebar.reconciler import scratch_retention, ticket_planner
from rebar.reconciler.mutation import Mutation, MutationAction, MutationDirection
from rebar.reconciler.mutation_payloads import OutboundUpdatePayload

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
APPLIER_PATH = SCRIPTS_DIR.parent / "reconciler" / "applier.py"
ACLI_PATH = SCRIPTS_DIR.parent / "reconciler" / "adapters" / "jira" / "acli.py"

# acli.py imports ``from rebar.reconciler.adapters.jira.adf import text_to_adf``;
# mirror the package-bootstrap pattern from test_applier_assignee_soft_fail.py
# so the loader chain resolves under any cwd.
_ADF_PATH = SCRIPTS_DIR.parent / "reconciler" / "adapters" / "jira" / "adf.py"
# acli.py also imports ``from rebar.reconciler.adapters.jira.comment_limits import ...``
# (bug 6afc-20ee-84e5-4dd5). Bootstrap it explicitly alongside adf so the loader
# chain resolves regardless of which sibling test first registered the
# ``rebar.reconciler`` namespace stub.
_CL_PATH = SCRIPTS_DIR.parent / "reconciler" / "adapters" / "jira" / "comment_limits.py"


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


@pytest.fixture(scope="module")
def applier_mod() -> Iterator[ModuleType]:
    name = "applier_stale_binding_404"
    mod = importlib.import_module("rebar.reconciler.applier")
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def acli_mod() -> Iterator[ModuleType]:
    name = "acli_stale_binding_404"
    mod = importlib.import_module("rebar.reconciler.adapters.jira.acli")
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


def _make_fake_acli(acli_mod: ModuleType, client: MagicMock) -> MagicMock:
    """Return the fake transport ``client`` directly (S4: ``_load_acli`` now returns
    the backend transport, not a module).

    apply_handlers catches ``AssigneeNotFoundError`` via a direct import from
    ``rebar.reconciler.adapters.jira.acli_subprocess`` — the same real class the test's client
    raises (the shadow ``rebar.reconciler`` package __path__'s to the real source
    dir), so the soft-fail path resolves to a real BaseException subclass.
    """
    return client


def _make_404() -> urllib.error.HTTPError:
    # Mirrors the production str() form "HTTP Error 404: Not Found".
    return urllib.error.HTTPError(
        url="https://example.atlassian.net/rest/api/3/issue/DIG-5305/transitions",
        code=404,
        msg="Not Found",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


def _read_manifest_outcomes(repo_root: Path, pass_id: str) -> list[dict]:
    manifest_path = scratch_retention.snapshots_dir(repo_root) / f"{pass_id}.manifest.json"
    if not manifest_path.is_file():
        return []
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return data.get("mutations", [])


def _update(key: str, fields: dict, *, local_id: str | None = None, **extra) -> Mutation:
    """A typed outbound update Mutation (``extra``: comments/labels/links tuples)."""
    return Mutation(
        MutationDirection.outbound,
        MutationAction.update,
        key,
        OutboundUpdatePayload(changed_fields=fields, local_id=local_id, **extra),
        {},
    )


def test_http_404_on_update_soft_fails_batch_continues(
    applier_mod: ModuleType,
    acli_mod: ModuleType,
    tmp_path: Path,
) -> None:
    """Record a stale 404 and continue to the valid sibling update."""
    pass_id = f"test-pass-404-{int(time.time())}"

    good_mutation = _update("DIG-4275", {"summary": "still works"}, local_id="good-local-id")
    stale_mutation = _update(
        "DIG-5305",  # deleted probe ticket, stale binding (1e08)
        {"status": "in_progress"},
        local_id="stale-local-id",
    )

    fake_client = MagicMock()

    def _update_issue_side_effect(issue_key, **kwargs):
        if issue_key == "DIG-5305":
            raise _make_404()
        return {"key": issue_key, "ok": True}

    fake_client.update_issue.side_effect = _update_issue_side_effect
    fake_acli_mod = _make_fake_acli(acli_mod, fake_client)

    with patch.object(applier_mod, "_load_acli", return_value=fake_acli_mod):
        try:
            applier_mod.apply(
                [good_mutation, stale_mutation],
                pass_id,
                repo_root=tmp_path,
                ticket_plans=_plans([good_mutation, stale_mutation]),
            )
        except urllib.error.HTTPError as exc:
            pytest.fail(
                f"applier.apply propagated HTTPError 404 instead of soft-failing the batch: {exc!r}"
            )

    # Both mutations were attempted; the good one DID run.
    update_calls = list(fake_client.update_issue.call_args_list)
    assert len(update_calls) == 2, (
        f"both mutations should have been attempted; got {len(update_calls)} calls"
    )

    # The 404 is recorded in the batch outcome for DIG-5305.
    outcomes = _read_manifest_outcomes(tmp_path, pass_id)
    stale_outcomes = [o for o in outcomes if o.get("key") == "DIG-5305"]
    assert stale_outcomes, f"expected an outcome for DIG-5305; got {outcomes}"
    assert stale_outcomes[0].get("error"), (
        f"DIG-5305 outcome must record the 404 error; got {stale_outcomes[0]}"
    )
    assert "404" in str(stale_outcomes[0]["error"]), (
        f"recorded error should name the 404; got {stale_outcomes[0]['error']!r}"
    )

    # The good mutation succeeded: assert BOTH the absence of an error AND a
    # positive success marker (the recorded ``result`` payload). Asserting only
    # the error's absence would pass vacuously if a regression nulled the
    # result, so pin the success indicator explicitly.
    good_outcomes = [o for o in outcomes if o.get("key") == "DIG-4275"]
    assert good_outcomes and not good_outcomes[0].get("error"), (
        f"good mutation should have no error; got {good_outcomes}"
    )
    assert good_outcomes[0].get("result") == {"key": "DIG-4275", "ok": True}, (
        "good mutation outcome must record the positive update_issue result, "
        f"not just the absence of an error; got {good_outcomes[0]}"
    )


def test_non_404_http_error_still_propagates(
    applier_mod: ModuleType,
    acli_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only 404 is softened. A 500 (or any non-404 HTTPError) must keep the
    current behavior and propagate — we do not blanket-catch HTTP errors.

    ADR 0103 S3: the handler makes ONE attempt, so the raw 500 propagates from the
    first call with no backoff in between (no handler retry, no coordinator replay).
    """
    pass_id = f"test-pass-500-{int(time.time())}"
    mutation = _update("DIG-9999", {"status": "in_progress"}, local_id="five-hundred-id")
    fake_client = MagicMock()
    fake_client.update_issue.side_effect = urllib.error.HTTPError(
        url="https://example.atlassian.net/rest/api/3/issue/DIG-9999/transitions",
        code=500,
        msg="Internal Server Error",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    fake_acli_mod = _make_fake_acli(acli_mod, fake_client)

    # RECORD any backoff instead of sleeping it: a handler-level retry would show up
    # here as a sleep before the second attempt.
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)

    with patch.object(applier_mod, "_load_acli", return_value=fake_acli_mod):
        with pytest.raises(urllib.error.HTTPError):
            applier_mod.apply(
                [mutation], pass_id, repo_root=tmp_path, ticket_plans=_plans([mutation])
            )

    assert fake_client.update_issue.call_count == 1, (
        f"a 500 must reach the transport once; got {fake_client.update_issue.call_count}"
    )
    assert delays == [], f"no backoff may precede a retry that no longer exists; got {delays}"


# ── RP-03 S3 T1: the coordinator's provider-neutral isolation of this same 404 ───────
#
# The production bug above is the raw-404 escape. S3 T1's ``failure_policy`` gives the
# non-create coordinator a provider-neutral classification of exactly this status code
# so a stale-binding 404 on an UPDATE is a ticket-scoped (isolatable) permanent failure,
# while a 404 on a DELETE is idempotent already-gone success — never a broad abort. This
# regression pins that mapping so the ticket-local-isolation contract (AC4) cannot
# silently regress into a pass-fatal or over-broad failure.

_FAILURE_POLICY_PATH = SCRIPTS_DIR.parent / "reconciler" / "failure_policy.py"
_OPERATION_OUTCOME_PATH = SCRIPTS_DIR.parent / "reconciler" / "operation_outcome.py"


@pytest.fixture(scope="module")
def failure_policy_mod() -> Iterator[ModuleType]:
    outcome_name = "operation_outcome_stale_binding_404"
    importlib.import_module("rebar.reconciler.operation_outcome")
    name = "failure_policy_stale_binding_404"
    mod = importlib.import_module("rebar.reconciler.failure_policy")
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)
        sys.modules.pop(outcome_name, None)


def test_stale_binding_404_update_is_ticket_local_permanent(failure_policy_mod: ModuleType) -> None:
    """The exact DIG-5305 scenario: a 404 on an ``update`` classifies as a ticket-scoped
    permanent failure (``permanent`` status, ``ticket`` scope) so the coordinator isolates
    the one ticket instead of aborting the pass or halting a broad scope."""
    status, scope = failure_policy_mod.classify_http_error(404, "update")
    assert status == "permanent"
    assert scope.value == "ticket"
    # A ticket scope is NOT broad — it cannot stop sibling tickets/endpoints.
    assert not failure_policy_mod.is_broad_scope(scope)


def test_already_gone_404_delete_is_idempotent_success(failure_policy_mod: ModuleType) -> None:
    """A 404 on a ``delete`` is idempotent already-gone success (``already_satisfied``,
    applied bucket) — the desired postcondition already holds."""
    status, scope = failure_policy_mod.classify_http_error(404, "delete")
    assert status == "already_satisfied"
    assert scope.value == "ticket"
    disposition = failure_policy_mod.status_to_disposition(status)
    assert failure_policy_mod.bucket_for(disposition) == "applied"
