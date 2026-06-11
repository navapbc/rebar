"""RED behavioral tests for ticket_reducer/marker.py API.

These tests are RED — they test functionality that does not yet exist.
All test functions must FAIL until ticket_reducer/marker.py is implemented.

The marker module is expected to expose three callables:
    write_marker(ticket_dir: Path) -> None
    remove_marker(ticket_dir: Path) -> None
    check_marker(ticket_dir: Path) -> bool

The marker file is: <ticket_dir>/.archived

Test: python3 -m pytest tests/scripts/test_ticket_reducer_marker.py
All tests must return non-zero until marker.py is implemented.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import helpers — marker.py does not exist yet (RED state)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"

# Ensure the scripts directory (which contains ticket_reducer package) is on sys.path
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Test 0 (SC8): Importability of write_marker, remove_marker, check_marker
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_marker_imports_from_package() -> None:
    """write_marker, remove_marker, check_marker must be importable from ticket_reducer.

    RED: marker.py does not exist yet — ImportError expected until implemented.
    SC8: covers import contract for all three public functions.
    """
    from ticket_reducer import check_marker, remove_marker, write_marker  # noqa: F401

    assert callable(write_marker), "write_marker must be callable"
    assert callable(remove_marker), "remove_marker must be callable"
    assert callable(check_marker), "check_marker must be callable"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ticket_dir(tmp_path: Path) -> Path:
    """Return a fresh temporary directory representing a ticket directory."""
    return tmp_path


# ---------------------------------------------------------------------------
# Test 1: write_marker creates .archived file
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_write_marker_creates_file(ticket_dir: Path) -> None:
    """Given a temp ticket dir with no .archived file, when write_marker is called,
    then <ticket_dir>/.archived file exists on disk.

    RED: marker.py does not exist yet.
    """
    from ticket_reducer import write_marker

    marker_path = ticket_dir / ".archived"
    assert not marker_path.exists(), "Pre-condition: .archived must not exist"

    write_marker(ticket_dir)

    assert marker_path.exists(), (
        f".archived was not created at {marker_path} — "
        "implement write_marker to make this test pass."
    )


# ---------------------------------------------------------------------------
# Test 2: check_marker returns True after write_marker
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_check_marker_true_after_write(ticket_dir: Path) -> None:
    """Given a temp ticket dir where write_marker was called, when check_marker is
    called, then it returns True.

    RED: marker.py does not exist yet.
    """
    from ticket_reducer import check_marker, write_marker

    write_marker(ticket_dir)
    result = check_marker(ticket_dir)

    assert result is True, (
        f"check_marker returned {result!r} after write_marker — "
        "implement check_marker to make this test pass."
    )


# ---------------------------------------------------------------------------
# Test 3: check_marker returns False without marker
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_check_marker_false_without_marker(ticket_dir: Path) -> None:
    """Given a temp ticket dir with no .archived file, when check_marker is called,
    then it returns False.

    RED: marker.py does not exist yet.
    """
    from ticket_reducer import check_marker

    assert not (ticket_dir / ".archived").exists(), (
        "Pre-condition: .archived must not exist"
    )

    result = check_marker(ticket_dir)

    assert result is False, (
        f"check_marker returned {result!r} when no .archived file present — "
        "implement check_marker to make this test pass."
    )


# ---------------------------------------------------------------------------
# Test 4: remove_marker deletes .archived file
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_remove_marker_deletes_file(ticket_dir: Path) -> None:
    """Given a temp ticket dir with a .archived file, when remove_marker is called,
    then the .archived file no longer exists.

    RED: marker.py does not exist yet.
    """
    from ticket_reducer import remove_marker

    marker_path = ticket_dir / ".archived"
    marker_path.touch()
    assert marker_path.exists(), "Pre-condition: .archived must exist"

    remove_marker(ticket_dir)

    assert not marker_path.exists(), (
        f".archived still exists at {marker_path} after remove_marker — "
        "implement remove_marker to make this test pass."
    )


# ---------------------------------------------------------------------------
# Test 5: remove_marker is idempotent (no error when .archived absent)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_remove_marker_idempotent(ticket_dir: Path) -> None:
    """Given a temp ticket dir with no .archived file, when remove_marker is called,
    then no error is raised and the directory state is unchanged.

    RED: marker.py does not exist yet.
    """
    from ticket_reducer import remove_marker

    assert not (ticket_dir / ".archived").exists(), (
        "Pre-condition: .archived must not exist"
    )

    # Must not raise
    try:
        remove_marker(ticket_dir)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"remove_marker raised {type(exc).__name__}: {exc} when .archived absent — "
            "implement remove_marker to be idempotent."
        )

    assert not (ticket_dir / ".archived").exists(), (
        "Directory state changed unexpectedly after idempotent remove_marker call."
    )


# ---------------------------------------------------------------------------
# Test 6: write_marker error tolerance (non-existent parent dir)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_write_marker_error_tolerance(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:  # type: ignore[type-arg]
    """Given write_marker is called on a ticket_dir where .archived cannot be created
    (e.g., dir is a non-existent path), when write_marker is called, then no exception
    is raised (the function logs to stderr and returns gracefully).

    RED: marker.py does not exist yet.
    """
    from ticket_reducer import write_marker

    non_existent_dir = tmp_path / "does" / "not" / "exist"
    assert not non_existent_dir.exists(), "Pre-condition: directory must not exist"

    # Must not raise — error tolerance contract
    try:
        write_marker(non_existent_dir)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"write_marker raised {type(exc).__name__}: {exc} for non-existent dir — "
            "implement write_marker to be error-tolerant (log to stderr, return gracefully)."
        )
