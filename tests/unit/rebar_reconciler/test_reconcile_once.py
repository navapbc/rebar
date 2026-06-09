"""Tests for rebar_reconciler/reconcile.py reconcile_once().

Covers:
- Idempotency: two consecutive reconcile_once() calls with unchanged remote
  both produce mutation_count=0 (second call sees prev==curr snapshot).
- EXCLUDED_FIELDS filter: a change only in an excluded field produces
  mutation_count=0 (excluded fields do not drive mutations).
- Real field convergence: a genuine field change produces mutation_count=1
  after the first pass (regression guard against over-filtering).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
RECONCILE_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "reconcile.py"
)
FETCHER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "fetcher.py"
)
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def reconcile_mod():
    """Load reconcile.py, failing all tests if absent."""
    if not RECONCILE_PATH.exists():
        pytest.fail(
            f"reconcile.py not found at {RECONCILE_PATH} — "
            "implement the module to make tests pass."
        )
    return _load_module("reconcile", RECONCILE_PATH)


@pytest.fixture(scope="module")
def fetcher_mod():
    """Load fetcher.py."""
    return _load_module("reconcile_fetcher", FETCHER_PATH)


@pytest.fixture(scope="module")
def applier_mod():
    """Load applier.py."""
    return _load_module("reconcile_applier", APPLIER_PATH)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_acli_module(issues: list[dict]) -> types.ModuleType:
    """Return a stub acli_integration module whose AcliClient returns issues."""

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def search_issues(self, jql: str, **kwargs) -> list[dict]:
            return list(issues)

        def create_issue(self, fields: dict) -> dict:
            return {"key": "DIG-NEW"}

        def update_issue(self, key: str, **fields) -> dict:
            # F3: applier unpacks fields as kwargs (real signature is
            # update_issue(jira_key, **kwargs)).
            return {"key": key}

        def transition_issue(self, key: str, status: str) -> None:
            return None

        # Bug 85a1 / Gap 1+5+8: create_one + update_one now dispatch labels,
        # comments, identity writes, and unassign-via-REST. The stub accepts
        # these as no-ops so reconcile_once tests don't crash on AttributeError.
        def add_label(self, key: str, label: str) -> None:
            return None

        def remove_label(self, key: str, label: str) -> None:
            return None

        def add_comment(self, key: str, body: str) -> dict:
            return {"id": "stub-comment"}

        def set_entity_property(self, key: str, prop: str, value) -> None:
            return None

        def delete_issue(self, key: str) -> None:
            return None

        def unassign_issue(self, key: str) -> None:
            return None

        def transition_issue_by_name(self, key: str, target: str) -> None:
            return None

    mock_mod = types.ModuleType("acli_integration")
    mock_mod.AcliClient = _Client
    return mock_mod


def _make_ok_concurrency() -> types.ModuleType:
    """Return a stub _concurrency module that always reports ok=True."""
    from dataclasses import dataclass
    from typing import Any

    @dataclass
    class _ConcurrencyEvent:
        kind: str
        message: str = ""
        attempt: int = 0

    @dataclass
    class _Result:
        ok: bool
        event: _ConcurrencyEvent | None = None
        value: Any = None

    def _snapshot_head(repo_root: Path) -> str:
        return "aabbccdd" * 5

    def _rebase_retry(repo_root, write_fn, *, max_attempts=3):
        write_fn()
        return _Result(ok=True)

    fake = types.ModuleType("_concurrency")
    fake.ConcurrencyEvent = _ConcurrencyEvent
    fake.Result = _Result
    fake.snapshot_head = _snapshot_head
    fake.rebase_retry = _rebase_retry
    return fake


def _make_stable_issues() -> list[dict]:
    """A small stable list of Jira issues with no EXCLUDED_FIELDS."""
    return [
        {
            "key": "DIG-1",
            "fields": {
                "summary": "Implement login",
                "status": {"name": "In Progress"},
                "issuetype": {"name": "Story"},
            },
        },
        {
            "key": "DIG-2",
            "fields": {
                "summary": "Write unit tests",
                "status": {"name": "To Do"},
                "issuetype": {"name": "Task"},
            },
        },
    ]


def _patch_acli_and_concurrency(fetcher_mod, applier_mod, issues: list[dict]):
    """Context manager: patch _load_acli in fetcher + applier, and _load_concurrency in applier."""
    import contextlib
    from unittest.mock import patch

    mock_acli = _make_acli_module(issues)
    ok_concurrency = _make_ok_concurrency()

    @contextlib.contextmanager
    def _ctx():
        with (
            patch.object(fetcher_mod, "_load_acli", return_value=mock_acli),
            patch.object(applier_mod, "_load_acli", return_value=mock_acli),
        ):
            original_load_concurrency = applier_mod._load_concurrency
            applier_mod._load_concurrency = lambda: ok_concurrency
            try:
                yield
            finally:
                applier_mod._load_concurrency = original_load_concurrency

    return _ctx()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_idempotency_two_passes_with_unchanged_remote(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod
):
    """Two consecutive reconcile_once() calls with unchanged remote both have mutation_count=0.

    The first pass initialises prev snapshot from empty ({}) so all issues appear
    as "create" mutations.  The second pass reads the prev snapshot written by the
    first pass and compares it against an identical current snapshot — producing
    zero mutations, proving idempotency.

    This test uses pass_id="idempotency-pass" for both calls so the prev file
    written by pass 1 is the one read by pass 2.
    """
    issues = _make_stable_issues()
    pass_id = "idempotency-pass"

    with _patch_acli_and_concurrency(fetcher_mod, applier_mod, issues):
        result1 = reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        result2 = reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)

    assert result2["mutation_count"] == 0, (
        f"Second pass over unchanged remote must produce mutation_count=0, "
        f"got {result2['mutation_count']}"
    )
    assert result1["pass_id"] == pass_id
    assert result2["pass_id"] == pass_id


def test_excluded_fields_change_does_not_drive_mutations(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod
):
    """A change only in an EXCLUDED_FIELDS field produces mutation_count=0.

    Pass 1: snapshot has issues with 'summary' field (real) and no excluded fields.
    Pass 2: same issues but with an 'local_id' field added (in EXCLUDED_FIELDS).
    The differ must ignore that change and emit zero mutations.
    """
    pass_id = "excluded-fields-pass"
    base_issues = [
        {
            "key": "DIG-10",
            "fields": {
                "summary": "Some issue",
                "status": {"name": "To Do"},
            },
        }
    ]
    # Second call's issues add an excluded field — should not trigger a mutation
    issues_with_excluded = [
        {
            "key": "DIG-10",
            "fields": {
                "summary": "Some issue",
                "status": {"name": "To Do"},
                "local_id": "abc-123",  # EXCLUDED_FIELDS member
            },
        }
    ]

    mock_acli_base = _make_acli_module(base_issues)
    mock_acli_excluded = _make_acli_module(issues_with_excluded)
    ok_concurrency = _make_ok_concurrency()

    from unittest.mock import patch

    # Pass 1: base issues (no excluded fields)
    with (
        patch.object(fetcher_mod, "_load_acli", return_value=mock_acli_base),
        patch.object(applier_mod, "_load_acli", return_value=mock_acli_base),
    ):
        applier_mod._load_concurrency_bak = applier_mod._load_concurrency
        applier_mod._load_concurrency = lambda: ok_concurrency
        try:
            reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            applier_mod._load_concurrency = applier_mod._load_concurrency_bak

    # Pass 2: same issues but with excluded field added
    with (
        patch.object(fetcher_mod, "_load_acli", return_value=mock_acli_excluded),
        patch.object(applier_mod, "_load_acli", return_value=mock_acli_excluded),
    ):
        applier_mod._load_concurrency = lambda: ok_concurrency
        try:
            result2 = reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            applier_mod._load_concurrency = applier_mod._load_concurrency_bak

    assert result2["mutation_count"] == 0, (
        f"Changing only an EXCLUDED_FIELDS field must produce mutation_count=0, "
        f"got {result2['mutation_count']}"
    )


def test_real_field_change_converges_after_one_pass(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod
):
    """A genuine field change produces mutation_count=1 after the first detecting pass.

    Pass 1 primes the prev snapshot (issues at v1).
    Pass 2 presents issues at v2 (summary changed) — must detect 1 mutation.
    This is a regression guard against over-filtering in the differ.
    """
    pass_id = "real-change-pass"
    issues_v1 = [
        {
            "key": "DIG-20",
            "fields": {
                "summary": "Original summary",
                "status": {"name": "To Do"},
            },
        }
    ]
    issues_v2 = [
        {
            "key": "DIG-20",
            "fields": {
                "summary": "CHANGED summary",  # real field changed
                "status": {"name": "To Do"},
            },
        }
    ]

    mock_acli_v1 = _make_acli_module(issues_v1)
    mock_acli_v2 = _make_acli_module(issues_v2)
    ok_concurrency = _make_ok_concurrency()

    from unittest.mock import patch

    # Pass 1: prime prev snapshot with v1
    with (
        patch.object(fetcher_mod, "_load_acli", return_value=mock_acli_v1),
        patch.object(applier_mod, "_load_acli", return_value=mock_acli_v1),
    ):
        applier_mod._load_concurrency_bak2 = applier_mod._load_concurrency
        applier_mod._load_concurrency = lambda: ok_concurrency
        try:
            reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            applier_mod._load_concurrency = applier_mod._load_concurrency_bak2

    # Pass 2: present v2 — differ must detect the real change
    with (
        patch.object(fetcher_mod, "_load_acli", return_value=mock_acli_v2),
        patch.object(applier_mod, "_load_acli", return_value=mock_acli_v2),
    ):
        applier_mod._load_concurrency = lambda: ok_concurrency
        try:
            result2 = reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            applier_mod._load_concurrency = applier_mod._load_concurrency_bak2

    assert result2["mutation_count"] == 1, (
        f"A genuine field change must produce mutation_count=1 on the detecting pass, "
        f"got {result2['mutation_count']}"
    )
