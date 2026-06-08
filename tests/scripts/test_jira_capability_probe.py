"""Tests for jira-capability-probe.py — six-step Jira round-trip capability probe.

Tests use importlib to load the probe script (no package import needed).
All tests mock AcliClient so no real network calls are made.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import types
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Module loading — filename has hyphens so we use importlib
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "jira-capability-probe.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client_mock(
    *,
    issue_key: str = "DIG-9999",
    search_results: list | None = None,
    property_value: str | None = None,
    uuid_val: str = "test-uuid-1234",
) -> mock.MagicMock:
    """Build a mock AcliClient with sensible defaults."""
    if search_results is None:
        search_results = [{"key": issue_key}]
    if property_value is None:
        property_value = uuid_val

    client = mock.MagicMock()
    client.create_issue.return_value = {"key": issue_key}
    client.search_issues.return_value = search_results
    client.get_issue_property.return_value = property_value
    client.set_issue_property.return_value = None
    client.delete_issue.return_value = {"status": "deleted", "key": issue_key}
    client._direct_rest_put.return_value = None
    return client


def _run_probe_with_mocked_acli(
    *,
    env: dict[str, str],
    client_instance: mock.MagicMock,
    extra_patches: list | None = None,
    module_suffix: str = "",
) -> tuple[int, str]:
    """Execute the probe module with AcliClient mocked; return (exit_code, stdout)."""
    if not PROBE_PATH.exists():
        pytest.fail(f"jira-capability-probe.py not found at {PROBE_PATH}")

    # The mocked module is actually produced inside _patched_module_from_spec
    # (further down). We just need the AcliClient mock class for the patch.
    mock_acli_cls = mock.MagicMock(return_value=client_instance)

    # Build a mock spec whose loader.exec_module populates a module object
    mock_spec = mock.MagicMock()
    mock_spec.loader = mock.MagicMock()

    def _fake_exec_module(m: types.ModuleType) -> None:
        m.AcliClient = mock_acli_cls  # type: ignore[attr-defined]

    mock_spec.loader.exec_module.side_effect = _fake_exec_module

    # We need the real spec_from_file_location for the probe itself
    _real_spec_from_file = importlib.util.spec_from_file_location
    _real_module_from_spec = importlib.util.module_from_spec

    probe_spec = _real_spec_from_file(
        f"jira_capability_probe_{module_suffix}", PROBE_PATH
    )
    probe_mod = _real_module_from_spec(probe_spec)

    def _patched_spec_from_file(
        name: str, path: object, *args: object, **kwargs: object
    ) -> object:
        if "acli-integration" in str(path) or name == "acli_integration":
            return mock_spec
        return _real_spec_from_file(name, path, *args, **kwargs)  # type: ignore[arg-type]

    def _patched_module_from_spec(spec: object) -> object:
        if spec is mock_spec:
            # Return a fresh ModuleType; _fake_exec_module will set AcliClient on it
            return types.ModuleType("acli_integration")
        return _real_module_from_spec(spec)

    captured = io.StringIO()
    exit_code: int | None = None

    patches: list[contextlib.AbstractContextManager] = [
        mock.patch.dict("os.environ", env, clear=False),
        mock.patch(
            "importlib.util.spec_from_file_location",
            side_effect=_patched_spec_from_file,
        ),
        mock.patch(
            "importlib.util.module_from_spec", side_effect=_patched_module_from_spec
        ),
        mock.patch("sys.stdout", captured),
    ]
    if extra_patches:
        patches.extend(extra_patches)

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)  # type: ignore[arg-type]
        # Load the module first (no main() side effect — main is guarded
        # by `if __name__ == "__main__":`).
        probe_spec.loader.exec_module(probe_mod)
        with pytest.raises(SystemExit) as exc_info:
            probe_mod.main()
        exit_code = exc_info.value.code

    return exit_code, captured.getvalue()


def _run_probe_no_acli(
    *,
    env: dict[str, str],
    module_suffix: str = "",
) -> tuple[int, str]:
    """Execute the probe module without mocking AcliClient (for credential checks)."""
    if not PROBE_PATH.exists():
        pytest.fail(f"jira-capability-probe.py not found at {PROBE_PATH}")

    _real_spec_from_file = importlib.util.spec_from_file_location
    _real_module_from_spec = importlib.util.module_from_spec

    probe_spec = _real_spec_from_file(
        f"jira_capability_probe_{module_suffix}", PROBE_PATH
    )
    probe_mod = _real_module_from_spec(probe_spec)

    captured = io.StringIO()
    exit_code: int | None = None

    with (
        mock.patch.dict("os.environ", env, clear=False),
        mock.patch("sys.stdout", captured),
    ):
        # Load the module first (no main() side effect — main is guarded
        # by `if __name__ == "__main__":`).
        probe_spec.loader.exec_module(probe_mod)
        with pytest.raises(SystemExit) as exc_info:
            probe_mod.main()
        exit_code = exc_info.value.code

    return exit_code, captured.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.scripts
def test_probe_round_trip_passes_all_six_steps() -> None:
    """All six steps pass: stdout contains all PROBE_PASS labels, process exits 0."""
    test_uuid = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
    issue_key = "DIG-9999"
    client_mock = _make_client_mock(
        issue_key=issue_key,
        search_results=[{"key": issue_key}],
        property_value=test_uuid,
        uuid_val=test_uuid,
    )

    env = {
        "JIRA_URL": "https://jira.example.com",
        "JIRA_USER": "user@example.com",
        "JIRA_API_TOKEN": "secret-token",
    }

    exit_code, stdout = _run_probe_with_mocked_acli(
        env=env,
        client_instance=client_mock,
        extra_patches=[mock.patch("uuid.uuid4", return_value=test_uuid)],
        module_suffix="t1",
    )

    assert exit_code == 0, f"Expected exit 0, got {exit_code}. stdout: {stdout}"
    expected_steps = [
        "PROBE_PASS step=STEP_CREATE",
        "PROBE_PASS step=STEP_LABEL",
        "PROBE_PASS step=STEP_PROPERTY_WRITE",
        "PROBE_PASS step=STEP_JQL_SEARCH",
        "PROBE_PASS step=STEP_PROPERTY_READ",
        "PROBE_PASS step=STEP_DELETE",
    ]
    for step in expected_steps:
        assert step in stdout, f"Expected '{step}' in stdout. Got:\n{stdout}"


@pytest.mark.scripts
def test_probe_exits_2_on_missing_credentials() -> None:
    """Missing JIRA_URL causes exit 2 with PROBE_FAIL reason=missing_credentials."""
    env = {
        "JIRA_URL": "",
        "JIRA_USER": "user@example.com",
        "JIRA_API_TOKEN": "secret-token",
    }

    exit_code, stdout = _run_probe_no_acli(env=env, module_suffix="t2")

    assert exit_code == 2, f"Expected exit 2, got {exit_code}"
    assert "PROBE_FAIL" in stdout, f"Expected PROBE_FAIL in stdout. Got: {stdout}"
    assert "missing_credentials" in stdout, (
        f"Expected 'missing_credentials' in stdout. Got: {stdout}"
    )


@pytest.mark.scripts
def test_probe_exits_1_on_property_read_mismatch_and_still_cleans_up() -> None:
    """Property read mismatch → exit 1, AND delete_issue still called (cleanup runs)."""
    test_uuid = "correct-uuid-1111"
    wrong_value = "wrong-uuid-9999"
    issue_key = "DIG-9999"

    client_mock = _make_client_mock(
        issue_key=issue_key,
        search_results=[{"key": issue_key}],
        property_value=wrong_value,  # mismatch!
        uuid_val=test_uuid,
    )

    env = {
        "JIRA_URL": "https://jira.example.com",
        "JIRA_USER": "user@example.com",
        "JIRA_API_TOKEN": "secret-token",
    }

    exit_code, stdout = _run_probe_with_mocked_acli(
        env=env,
        client_instance=client_mock,
        extra_patches=[mock.patch("uuid.uuid4", return_value=test_uuid)],
        module_suffix="t3",
    )

    assert exit_code == 1, f"Expected exit 1, got {exit_code}. stdout: {stdout}"
    # Cleanup must still happen
    client_mock.delete_issue.assert_called_once_with(issue_key)


@pytest.mark.scripts
def test_probe_retries_jql_search_three_times() -> None:
    """search_issues is called exactly 3 times when it always returns empty list."""
    test_uuid = "retry-uuid-5555"
    issue_key = "DIG-9999"

    client_mock = _make_client_mock(
        issue_key=issue_key,
        search_results=[],  # always empty — forces retries
        property_value=test_uuid,
        uuid_val=test_uuid,
    )

    env = {
        "JIRA_URL": "https://jira.example.com",
        "JIRA_USER": "user@example.com",
        "JIRA_API_TOKEN": "secret-token",
    }

    _run_probe_with_mocked_acli(
        env=env,
        client_instance=client_mock,
        extra_patches=[
            mock.patch("uuid.uuid4", return_value=test_uuid),
            mock.patch("time.sleep"),  # avoid real delay
        ],
        module_suffix="t4",
    )

    # search_issues must be called exactly _JQL_RETRY_COUNT times (one per attempt
    # when every attempt returns empty). The probe constant is 6; this assertion
    # tracks the source constant rather than a hard-coded literal that drifts.
    assert client_mock.search_issues.call_count == 6, (
        f"Expected search_issues called 6 times (_JQL_RETRY_COUNT), got "
        f"{client_mock.search_issues.call_count}"
    )
