"""RED tests for _apply_outbound_create rollback path (task dd-3 / 5b41-a748).

When the typed leaf ``_apply_outbound_create`` attempts a create_issue call
through ``_call_with_retry`` and the call ultimately raises, the leaf MUST
roll back by invoking ``client.delete_issue`` through the SAME retry helper
(so transient failures during rollback also get retried), then re-raise the
original create exception.

These tests target the typed-mutation leaf, not the legacy ``create_one``
helper (covered separately by ``test_applier_rollback.py``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
)
MUTATION_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mutation.py"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    return _load_module("applier_outbound_create_rollback", APPLIER_PATH)


@pytest.fixture(scope="module")
def mutation_mod(applier):
    # IMPORTANT: load the mutation module via the applier's own loader so the
    # enum class identities (used in `is` comparisons inside _direction_guard)
    # match the ones the leaf sees. Loading mutation.py fresh in the test would
    # produce a distinct MutationDirection class and fail direction_guard.
    return applier._load_mutation_module()


def _make_outbound_create_mutation(mutation_mod, *, target: str = "LOCAL-A", key_hint: str = "PROJ-1"):
    return mutation_mod.Mutation(
        direction=mutation_mod.MutationDirection.outbound,
        action=mutation_mod.MutationAction.create,
        target=target,
        payload={"summary": "test", "key_hint": key_hint},
        provenance={"source": "test"},
    )


def test_rollback_invokes_delete_via_call_with_retry(applier, mutation_mod):
    """When create_issue raises, rollback must invoke delete_issue via _call_with_retry."""
    client = MagicMock()
    client.create_issue.side_effect = RuntimeError("create failed")

    mutation = _make_outbound_create_mutation(mutation_mod)

    from rebar_reconciler import apply_outbound  # point-of-use: leaf calls retry here
    real_call_with_retry = apply_outbound._call_with_retry
    captured: list[tuple] = []

    def spy(fn, *args, **kwargs):
        captured.append((fn, args, kwargs))
        return real_call_with_retry(fn, *args, **kwargs)

    with patch.object(apply_outbound, "_call_with_retry", side_effect=spy):
        with pytest.raises(RuntimeError, match="create failed"):
            applier._apply_outbound_create(mutation, client=client)

    delete_calls = [c for c in captured if c[0] is client.delete_issue]
    assert delete_calls, (
        f"delete_issue was not invoked via _call_with_retry. "
        f"Recorded calls: {[(c[0], c[1], c[2]) for c in captured]}"
    )
    # The delete call must reference the created-issue key (key_hint in payload).
    delete_args = delete_calls[0][1]
    assert "PROJ-1" in delete_args, (
        f"delete_issue should be invoked with the issue key 'PROJ-1', got args={delete_args}"
    )


def test_create_exception_reraised(applier, mutation_mod):
    """The ORIGINAL create exception (not any rollback exception) propagates."""
    client = MagicMock()
    original = RuntimeError("ORIGINAL")
    client.create_issue.side_effect = original

    mutation = _make_outbound_create_mutation(mutation_mod)

    with pytest.raises(RuntimeError) as exc_info:
        applier._apply_outbound_create(mutation, client=client)

    # Must be the original create error, not a rollback / retry-exhausted error.
    assert "ORIGINAL" in str(exc_info.value)


def test_rollback_swallows_delete_errors(applier, mutation_mod):
    """If delete_issue itself raises during rollback, the ORIGINAL create error still propagates."""
    client = MagicMock()
    original = RuntimeError("ORIGINAL_CREATE")
    client.create_issue.side_effect = original
    client.delete_issue.side_effect = RuntimeError("delete also failed")

    mutation = _make_outbound_create_mutation(mutation_mod)

    with pytest.raises(RuntimeError) as exc_info:
        applier._apply_outbound_create(mutation, client=client)

    assert "ORIGINAL_CREATE" in str(exc_info.value)
    assert "delete also failed" not in str(exc_info.value)


def test_no_rollback_when_create_succeeds(applier, mutation_mod):
    """On a successful create, delete_issue must not be invoked."""
    client = MagicMock()
    client.create_issue.return_value = {"key": "PROJ-1"}

    mutation = _make_outbound_create_mutation(mutation_mod)

    result = applier._apply_outbound_create(mutation, client=client)

    client.delete_issue.assert_not_called()
    assert result.direction is mutation.direction
    assert result.action is mutation.action
