"""Unit tests for _concurrency.py.

Tests cover:
  - test_snapshot_head_returns_nonempty_string: snapshot_head() on a real tmp
    git repo returns a non-empty hex SHA string.
  - test_rebase_retry_ok_when_write_succeeds: rebase_retry() returns
    Result(ok=True) when write_fn succeeds and HEAD is stable.
  - test_rebase_retry_abort_due_to_error: rebase_retry() returns
    Result(ok=False, event.kind='abort_due_to_error') when write_fn raises.
  - test_rebase_retry_abort_due_to_drift: rebase_retry() returns
    Result(ok=False, event.kind='abort_due_to_drift') when HEAD changes between
    the before-capture and the after-check.
  - test_concurrency_event_kind_values: ConcurrencyEvent accepts each of the
    three expected kind strings without error.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_concurrency.py"
)


def _load_module() -> ModuleType:
    import sys

    spec = importlib.util.spec_from_file_location("_concurrency", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules before exec so that dataclass annotation resolution
    # (which calls sys.modules.get(cls.__module__)) works in Python 3.14+.
    sys.modules["_concurrency"] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop("_concurrency", None)
        raise
    return mod


@pytest.fixture(scope="module")
def concurrency() -> ModuleType:
    """Return the _concurrency module; fail all tests if absent."""
    if not MODULE_PATH.exists():
        pytest.fail(
            f"_concurrency.py not found at {MODULE_PATH} — "
            "implement the module to make tests pass."
        )
    return _load_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repository with one commit and return its root."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    readme = tmp_path / "README.md"
    readme.write_text("hello\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_snapshot_head_returns_nonempty_string(concurrency, tmp_git_repo: Path) -> None:
    """snapshot_head() on a real tmp git repo returns a non-empty hex string."""
    sha = concurrency.snapshot_head(tmp_git_repo)
    assert isinstance(sha, str)
    assert len(sha) > 0
    # Should look like a hex SHA (at least 7 chars)
    assert all(c in "0123456789abcdef" for c in sha.lower())


def test_snapshot_head_returns_sentinel_on_empty_repo(concurrency, tmp_path: Path) -> None:
    """F9 regression: snapshot_head must return EMPTY_REPO_SENTINEL on a bare
    repo (``git init`` with no commits), not raise CalledProcessError.

    Before F9, the second subprocess.run used check=True; on a bare repo where
    neither ``tickets`` nor ``HEAD`` resolves, the call raised and the
    reconciler could not bootstrap. The fix returns a sentinel that drift
    detection treats as stable until the first commit lands.
    """
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)

    # Must NOT raise
    sha = concurrency.snapshot_head(tmp_path)
    assert sha == concurrency.EMPTY_REPO_SENTINEL, (
        f"snapshot_head on a bare repo must return EMPTY_REPO_SENTINEL; got {sha!r}"
    )


def test_rebase_retry_ok_when_write_succeeds(concurrency, tmp_git_repo: Path) -> None:
    """rebase_retry() returns Result(ok=True) when write_fn succeeds and HEAD is stable."""
    sentinel = object()

    def write_fn():
        return sentinel

    result = concurrency.rebase_retry(tmp_git_repo, write_fn)
    assert result.ok is True
    assert result.event is None
    assert result.value is sentinel


def test_rebase_retry_abort_due_to_error(concurrency, tmp_git_repo: Path) -> None:
    """rebase_retry() returns Result(ok=False, event.kind='abort_due_to_error') when write_fn raises."""

    def write_fn():
        raise RuntimeError("simulated write failure")

    result = concurrency.rebase_retry(tmp_git_repo, write_fn)
    assert result.ok is False
    assert result.event is not None
    assert result.event.kind == "abort_due_to_error"
    assert "simulated write failure" in result.event.message
    assert result.event.attempt == 1


def test_rebase_retry_reject_and_reschedule_when_all_attempts_drift(
    concurrency, tmp_git_repo: Path
) -> None:
    """rebase_retry() returns Result(ok=False, event.kind='reject_and_reschedule')
    when HEAD drifts on every attempt and max_attempts is exhausted.

    Drift is retryable — the loop re-pins and retries. Only after every attempt
    drifts does the result settle on reject_and_reschedule.
    """
    # snapshot_head is called twice per attempt: once before, once after.
    # Returning a different SHA each call guarantees drift on every attempt.
    sha_seq = iter(["aaaa" * 10, "bbbb" * 10, "cccc" * 10,
                    "dddd" * 10, "eeee" * 10, "ffff" * 10])

    def fake_snapshot_head(repo_root):  # noqa: ARG001
        return next(sha_seq)

    def write_fn():
        return "write_result"

    original_snapshot_head = concurrency.snapshot_head
    concurrency.snapshot_head = fake_snapshot_head
    try:
        result = concurrency.rebase_retry(tmp_git_repo, write_fn, max_attempts=3)
    finally:
        concurrency.snapshot_head = original_snapshot_head

    assert result.ok is False
    assert result.event is not None
    assert result.event.kind == "reject_and_reschedule"
    assert result.event.attempt == 3


def test_rebase_retry_retries_on_drift_then_succeeds(
    concurrency, tmp_git_repo: Path
) -> None:
    """Regression for F1: rebase_retry must retry on drift.

    With max_attempts=3, 2 forced drifts followed by 1 stable HEAD must result
    in 3 attempts total and Result.ok=True. Before F1, the for-loop returned on
    every branch and max_attempts > 1 was dead code.
    """
    # Sequence of snapshot_head returns (two calls per attempt):
    #   attempt 1: before="aaaa", after="bbbb"  → drift, retry
    #   attempt 2: before="cccc", after="dddd"  → drift, retry
    #   attempt 3: before="eeee", after="eeee"  → stable, success
    sha_seq = iter(
        ["a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40, "e" * 40]
    )

    write_call_count = {"n": 0}

    def fake_snapshot_head(repo_root):  # noqa: ARG001
        return next(sha_seq)

    def write_fn():
        write_call_count["n"] += 1
        return f"write_{write_call_count['n']}"

    original_snapshot_head = concurrency.snapshot_head
    concurrency.snapshot_head = fake_snapshot_head
    try:
        result = concurrency.rebase_retry(tmp_git_repo, write_fn, max_attempts=3)
    finally:
        concurrency.snapshot_head = original_snapshot_head

    assert result.ok is True, (
        f"Expected ok=True after retry-then-success; got event={result.event}"
    )
    assert result.value == "write_3"
    assert write_call_count["n"] == 3, (
        f"write_fn must be invoked once per attempt (3 total); "
        f"got {write_call_count['n']}"
    )


def test_concurrency_event_kind_values(concurrency) -> None:
    """ConcurrencyEvent accepts each of the three expected kind strings."""
    for kind in ("abort_due_to_drift", "reject_and_reschedule", "abort_due_to_error"):
        evt = concurrency.ConcurrencyEvent(kind=kind, message="test", attempt=1)
        assert evt.kind == kind
