"""Unit tests for dso_reconciler/invariants.py.

Covers check_at_most_one_dso_local_id end-to-end against a mocked alert_store
and a mocked subprocess.run for the ticket CLI invocation. Does NOT load
reconcile.py — tests that exercise the reconcile→invariants integration live
in test_at_most_one_invariant.py (deferred to the core-pipeline PR because
that test loads reconcile.py too).
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INVARIANTS_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "invariants.py"
)


def _load_invariants() -> ModuleType:
    spec = importlib.util.spec_from_file_location("invariants", INVARIANTS_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def invariants() -> ModuleType:
    return _load_invariants()


@pytest.fixture
def mock_alert_store() -> MagicMock:
    """Mock alert_store module with is_deduped / append / patch_bug_filed."""
    m = MagicMock()
    m.is_deduped.return_value = False
    return m


def _snapshot_with_dup(jira_key: str = "DIG-100") -> dict:
    return {jira_key: {"dso_local_ids": ["id-a", "id-b"]}}


def _ok_cli_result(bug_id: str = "abc1-def2-1234-5678") -> MagicMock:
    """Mock a successful ticket-create.sh stdout.

    ticket-create.sh emits two lines on success: a human-readable summary
    followed by the canonical 16-hex ticket ID on its own line. The mocked
    stdout mirrors that format so the regex-based extractor in
    invariants._extract_ticket_id sees real-shape data.
    """
    r = MagicMock()
    r.returncode = 0
    r.stdout = f"Created ticket {bug_id}: at-most-one violation\n{bug_id}\n"
    r.stderr = ""
    return r


# ---------------------------------------------------------------------------
# Happy path — duplicate found, bug filed, alert patched
# ---------------------------------------------------------------------------


def test_dup_dso_local_ids_files_alert_and_bug(
    invariants: ModuleType, mock_alert_store: MagicMock, tmp_path: Path
) -> None:
    """A snapshot with duplicate dso_local_ids files one alert and one bug ticket."""
    with patch.object(invariants, "_load_alert_store", return_value=mock_alert_store):
        with patch.object(invariants.subprocess, "run", return_value=_ok_cli_result()):
            filed = invariants.check_at_most_one_dso_local_id(
                _snapshot_with_dup(), repo_root=tmp_path, ticket_cli="/fake/dso"
            )

    assert len(filed) == 1
    assert filed[0]["jira_key"] == "DIG-100"
    assert filed[0]["dedup_key"] == "bridge-alert:at-most-one:DIG-100"
    mock_alert_store.append.assert_called_once()
    mock_alert_store.patch_bug_filed.assert_called_once_with(
        "bridge-alert:at-most-one:DIG-100", "abc1-def2-1234-5678", tmp_path
    )


# ---------------------------------------------------------------------------
# No duplicates — no alerts, no bugs
# ---------------------------------------------------------------------------


def test_no_duplicates_files_nothing(
    invariants: ModuleType, mock_alert_store: MagicMock, tmp_path: Path
) -> None:
    """A clean snapshot triggers no alerts."""
    snap = {"DIG-1": {"dso_local_ids": ["only-one"]}, "DIG-2": {}}
    with patch.object(invariants, "_load_alert_store", return_value=mock_alert_store):
        filed = invariants.check_at_most_one_dso_local_id(
            snap, repo_root=tmp_path, ticket_cli="/fake/dso"
        )

    assert filed == []
    mock_alert_store.append.assert_not_called()
    mock_alert_store.patch_bug_filed.assert_not_called()


# ---------------------------------------------------------------------------
# Dedup short-circuit — already-filed alert is not re-filed
# ---------------------------------------------------------------------------


def test_dedup_short_circuits(
    invariants: ModuleType, mock_alert_store: MagicMock, tmp_path: Path
) -> None:
    """When is_deduped returns True, the violation is skipped."""
    mock_alert_store.is_deduped.return_value = True
    with patch.object(invariants, "_load_alert_store", return_value=mock_alert_store):
        with patch.object(invariants.subprocess, "run") as mock_run:
            filed = invariants.check_at_most_one_dso_local_id(
                _snapshot_with_dup(), repo_root=tmp_path, ticket_cli="/fake/dso"
            )

    assert filed == []
    mock_alert_store.append.assert_not_called()
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# CAP_PER_PASS — only 5 violations filed even when more exist
# ---------------------------------------------------------------------------


def test_cap_per_pass_limits_filings(
    invariants: ModuleType, mock_alert_store: MagicMock, tmp_path: Path
) -> None:
    """Only _CAP_PER_PASS=5 violations are filed per call; extras are skipped this pass."""
    snap = {f"DIG-{i}": {"dso_local_ids": ["a", "b"]} for i in range(10)}
    with patch.object(invariants, "_load_alert_store", return_value=mock_alert_store):
        with patch.object(invariants.subprocess, "run", return_value=_ok_cli_result()):
            filed = invariants.check_at_most_one_dso_local_id(
                snap, repo_root=tmp_path, ticket_cli="/fake/dso"
            )

    assert len(filed) == 5
    assert mock_alert_store.append.call_count == 5


# ---------------------------------------------------------------------------
# TimeoutExpired — alert is still written, stderr WARN surfaced, no
# patch_bug_filed call
# ---------------------------------------------------------------------------


def test_timeout_surfaces_warning_does_not_patch_bug(
    invariants: ModuleType,
    mock_alert_store: MagicMock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """TimeoutExpired during ticket-create surfaces a WARN to stderr and leaves the alert orphan-without-bug (next pass will see dedup; operators are expected to act on the WARN)."""
    with patch.object(invariants, "_load_alert_store", return_value=mock_alert_store):
        with patch.object(
            invariants.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="dso", timeout=30),
        ):
            filed = invariants.check_at_most_one_dso_local_id(
                _snapshot_with_dup(), repo_root=tmp_path, ticket_cli="/fake/dso"
            )

    # Violation IS recorded (alert appended, returned) even though ticket failed
    assert len(filed) == 1
    mock_alert_store.append.assert_called_once()
    mock_alert_store.patch_bug_filed.assert_not_called()

    err = capsys.readouterr().err
    assert "WARN" in err
    assert "bridge-alert:at-most-one:DIG-100" in err
    assert "timed out" in err


def test_oserror_surfaces_warning_does_not_patch_bug(
    invariants: ModuleType,
    mock_alert_store: MagicMock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FileNotFoundError (e.g., ticket_cli not on PATH) is treated like a transient CLI failure: surface WARN, leave alert without bug-ticket linkage, do NOT crash the loop."""
    with patch.object(invariants, "_load_alert_store", return_value=mock_alert_store):
        with patch.object(
            invariants.subprocess,
            "run",
            side_effect=FileNotFoundError("dso not on PATH"),
        ):
            filed = invariants.check_at_most_one_dso_local_id(
                _snapshot_with_dup(), repo_root=tmp_path, ticket_cli="/fake/dso"
            )

    assert len(filed) == 1
    mock_alert_store.append.assert_called_once()
    mock_alert_store.patch_bug_filed.assert_not_called()
    err = capsys.readouterr().err
    assert "WARN" in err
    assert "FileNotFoundError" in err


# ---------------------------------------------------------------------------
# Non-zero ticket-create exit — alert kept, WARN surfaced
# ---------------------------------------------------------------------------


def test_non_zero_exit_surfaces_warning(
    invariants: ModuleType,
    mock_alert_store: MagicMock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When ticket-create exits non-zero, the failure is surfaced but the alert is preserved."""
    bad_result = MagicMock()
    bad_result.returncode = 1
    bad_result.stdout = ""
    bad_result.stderr = "Invalid ticket type"
    with patch.object(invariants, "_load_alert_store", return_value=mock_alert_store):
        with patch.object(invariants.subprocess, "run", return_value=bad_result):
            filed = invariants.check_at_most_one_dso_local_id(
                _snapshot_with_dup(), repo_root=tmp_path, ticket_cli="/fake/dso"
            )

    assert len(filed) == 1
    mock_alert_store.append.assert_called_once()
    mock_alert_store.patch_bug_filed.assert_not_called()
    err = capsys.readouterr().err
    assert "exit=1" in err


# ---------------------------------------------------------------------------
# Programming errors (AttributeError, TypeError) now propagate — they no
# longer get silently swallowed by an over-broad except.
# ---------------------------------------------------------------------------


def test_programming_error_propagates(
    invariants: ModuleType, mock_alert_store: MagicMock, tmp_path: Path
) -> None:
    """An AttributeError (programming bug) inside subprocess.run is NOT swallowed."""
    with patch.object(invariants, "_load_alert_store", return_value=mock_alert_store):
        with patch.object(
            invariants.subprocess,
            "run",
            side_effect=AttributeError("simulated programming defect"),
        ):
            with pytest.raises(AttributeError, match="simulated programming defect"):
                invariants.check_at_most_one_dso_local_id(
                    _snapshot_with_dup(), repo_root=tmp_path, ticket_cli="/fake/dso"
                )


# ---------------------------------------------------------------------------
# Non-list dso_local_ids value is ignored (defensive read shape)
# ---------------------------------------------------------------------------


def test_non_list_dso_local_ids_is_ignored(
    invariants: ModuleType, mock_alert_store: MagicMock, tmp_path: Path
) -> None:
    """A snapshot entry whose dso_local_ids is not a list (or a single-element list) does not trigger a violation."""
    snap = {
        "DIG-A": {"dso_local_ids": "single-string-not-a-list"},
        "DIG-B": {"dso_local_ids": ["just-one"]},
        "DIG-C": {},  # no dso_local_ids at all
    }
    with patch.object(invariants, "_load_alert_store", return_value=mock_alert_store):
        filed = invariants.check_at_most_one_dso_local_id(
            snap, repo_root=tmp_path, ticket_cli="/fake/dso"
        )

    assert filed == []
    mock_alert_store.append.assert_not_called()


# ---------------------------------------------------------------------------
# Regression: ticket-ID extraction is regex-based, not whitespace-split based.
# Guards against the fragile `stdout.strip().split()[-1]` pattern that would
# return whatever final token appears in stdout — including title fragments
# or "ERROR" — and patch the alert with garbage.
# ---------------------------------------------------------------------------


def test_extract_ticket_id_canonical_format(invariants: ModuleType) -> None:
    """The regex extractor returns the canonical 16-hex ticket ID."""
    stdout = "Created ticket abc1-def2-1234-5678: title text\nabc1-def2-1234-5678\n"
    assert invariants._extract_ticket_id(stdout) == "abc1-def2-1234-5678"


def test_extract_ticket_id_garbage_returns_empty(invariants: ModuleType) -> None:
    """Garbage stdout (no canonical-format token) returns the empty string."""
    assert invariants._extract_ticket_id("ERROR") == ""
    assert invariants._extract_ticket_id("Created ticket: foo\n") == ""
    assert invariants._extract_ticket_id("") == ""
    # Almost-but-not-quite canonical: wrong group lengths.
    assert invariants._extract_ticket_id("abcd-12-345-6789") == ""


def test_extract_ticket_id_picks_last_canonical_match(
    invariants: ModuleType,
) -> None:
    """When multiple canonical IDs appear, the LAST one is returned (matches
    the final-line position in ticket-create.sh stdout)."""
    # Simulate human summary referencing an older ticket, then the canonical
    # line for the newly-created ticket.
    stdout = (
        "Created ticket aaaa-bbbb-cccc-dddd (alias of 1111-2222-3333-4444): t\n"
        "1111-2222-3333-4444\n"
    )
    assert (
        invariants._extract_ticket_id(stdout) == "1111-2222-3333-4444"
    )


def test_garbage_cli_output_leaves_alert_unpatched(
    invariants: ModuleType,
    mock_alert_store: MagicMock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the CLI exits 0 but stdout has no canonical ticket ID, the alert
    is NOT patched with a wrong value — patch_bug_filed must not be called,
    and a WARN is surfaced."""
    garbage_result = MagicMock()
    garbage_result.returncode = 0
    garbage_result.stdout = "ERROR: something weird\n"
    garbage_result.stderr = ""
    with patch.object(invariants, "_load_alert_store", return_value=mock_alert_store):
        with patch.object(invariants.subprocess, "run", return_value=garbage_result):
            filed = invariants.check_at_most_one_dso_local_id(
                _snapshot_with_dup(), repo_root=tmp_path, ticket_cli="/fake/dso"
            )

    # Violation IS recorded (alert was appended) but the bug-link patch is
    # skipped because no canonical ID could be extracted.
    assert len(filed) == 1
    mock_alert_store.append.assert_called_once()
    mock_alert_store.patch_bug_filed.assert_not_called()
    err = capsys.readouterr().err
    assert "WARN" in err
    assert "no canonical ticket ID" in err


def test_valid_cli_output_extracts_id_and_patches_alert(
    invariants: ModuleType, mock_alert_store: MagicMock, tmp_path: Path
) -> None:
    """When CLI stdout contains a canonical-format ID, it is extracted and
    used to patch the alert — guards against a regression to the
    whitespace-split[-1] approach that would return the wrong token if the
    title contains trailing tokens."""
    # Title is "at-most-one violation: DIG-100 has multiple dso_local_ids" —
    # if extraction reverted to split()[-1] of the FIRST line, it would return
    # "dso_local_ids", not the canonical ID.
    canonical_id = "9999-aaaa-bbbb-cccc"
    multi_line_stdout = (
        f"Created ticket some-alias ({canonical_id}): "
        f"at-most-one violation: DIG-100 has multiple dso_local_ids\n"
        f"{canonical_id}\n"
    )
    result = MagicMock()
    result.returncode = 0
    result.stdout = multi_line_stdout
    result.stderr = ""
    with patch.object(invariants, "_load_alert_store", return_value=mock_alert_store):
        with patch.object(invariants.subprocess, "run", return_value=result):
            filed = invariants.check_at_most_one_dso_local_id(
                _snapshot_with_dup(), repo_root=tmp_path, ticket_cli="/fake/dso"
            )

    assert len(filed) == 1
    mock_alert_store.patch_bug_filed.assert_called_once_with(
        "bridge-alert:at-most-one:DIG-100", canonical_id, tmp_path
    )
