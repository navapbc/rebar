"""Behavioral coverage for the reducer's archive-marker API."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Marker imports

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"

# Make the reducer package importable.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# Public imports


@pytest.mark.unit
@pytest.mark.scripts
def test_marker_imports_from_package() -> None:
    """The reducer package exports all three marker operations."""
    from rebar.reducer import check_marker, remove_marker, write_marker

    assert callable(write_marker), "write_marker must be callable"
    assert callable(remove_marker), "remove_marker must be callable"
    assert callable(check_marker), "check_marker must be callable"


@pytest.fixture
def ticket_dir(tmp_path: Path) -> Path:
    """Return an empty ticket directory."""
    return tmp_path


# Marker creation


@pytest.mark.unit
@pytest.mark.scripts
def test_write_marker_creates_file(ticket_dir: Path) -> None:
    """``write_marker`` creates ``.archived`` in the ticket directory."""
    from rebar.reducer import write_marker

    marker_path = ticket_dir / ".archived"
    assert not marker_path.exists(), "Pre-condition: .archived must not exist"

    write_marker(ticket_dir)

    assert marker_path.exists(), (
        f".archived was not created at {marker_path} — "
        "implement write_marker to make this test pass."
    )


# Post-write check


@pytest.mark.unit
@pytest.mark.scripts
def test_check_marker_true_after_write(ticket_dir: Path) -> None:
    """``check_marker`` returns true after ``write_marker``."""
    from rebar.reducer import check_marker, write_marker

    write_marker(ticket_dir)
    result = check_marker(ticket_dir)

    assert result is True, (
        f"check_marker returned {result!r} after write_marker — "
        "implement check_marker to make this test pass."
    )


# Absent marker check


@pytest.mark.unit
@pytest.mark.scripts
def test_check_marker_false_without_marker(ticket_dir: Path) -> None:
    """``check_marker`` returns false when ``.archived`` is absent."""
    from rebar.reducer import check_marker

    assert not (ticket_dir / ".archived").exists(), "Pre-condition: .archived must not exist"

    result = check_marker(ticket_dir)

    assert result is False, (
        f"check_marker returned {result!r} when no .archived file present — "
        "implement check_marker to make this test pass."
    )


# Marker removal


@pytest.mark.unit
@pytest.mark.scripts
def test_remove_marker_deletes_file(ticket_dir: Path) -> None:
    """``remove_marker`` deletes an existing ``.archived`` file."""
    from rebar.reducer import remove_marker

    marker_path = ticket_dir / ".archived"
    marker_path.touch()
    assert marker_path.exists(), "Pre-condition: .archived must exist"

    remove_marker(ticket_dir)

    assert not marker_path.exists(), (
        f".archived still exists at {marker_path} after remove_marker — "
        "implement remove_marker to make this test pass."
    )


# Idempotent removal


@pytest.mark.unit
@pytest.mark.scripts
def test_remove_marker_idempotent(ticket_dir: Path) -> None:
    """``remove_marker`` tolerates an absent marker."""
    from rebar.reducer import remove_marker

    assert not (ticket_dir / ".archived").exists(), "Pre-condition: .archived must not exist"

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


# Write-error tolerance


@pytest.mark.unit
@pytest.mark.scripts
def test_write_marker_error_tolerance(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
    """``write_marker`` reports an unwritable path without raising."""
    from rebar.reducer import write_marker

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
