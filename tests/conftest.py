"""Repo-wide pytest configuration.

Provides an autouse fixture that prevents tests from creating new top-level
entries in REPO_ROOT. Tests that write to disk must use ``tmp_path`` or
another sandboxed location. If a test leaks, the leak is cleaned up and the
test fails with a message naming the new entries.

This guard catches the most common leak shape — relative-path writes from
mis-routed tracker_dir/cwd handling (the failure mode that put
``depends_on/tkt-src3`` at the repo root). It does NOT catch writes that
target an existing top-level dir (e.g. ``src/rebar/_engine/x.json``); for
that level of guarantee, run ``git status --porcelain`` in CI.

Also provides a network-escape guard (bug 1c68) for tests/unit/** and
tests/scripts/**. Any test in those tiers that opens a real socket raises
``RuntimeError`` with a clear message. Tests that legitimately need network
access (none expected in these tiers) may opt out via
``@pytest.mark.allow_network``.
"""

from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers so pytest does not emit UnknownMarkWarning."""
    config.addinivalue_line(
        "markers",
        "allow_network: opt out of the network-escape guard for tests that "
        "legitimately require real network access (use sparingly; not expected "
        "in unit or scripts tiers).",
    )
    config.addinivalue_line(
        "markers",
        "unit: mark a test as a unit test.",
    )


# Directories whose tests are network-isolated by the socket guard.
_NETWORK_GUARDED_TIERS = (
    _REPO_ROOT / "tests" / "unit",
    _REPO_ROOT / "tests" / "scripts",
)


def _in_guarded_tier(item: pytest.Item) -> bool:
    """Return True if *item* lives under one of the network-guarded test dirs."""
    try:
        test_path = Path(item.fspath).resolve()
    except Exception:
        return False
    return any(test_path.is_relative_to(tier) for tier in _NETWORK_GUARDED_TIERS)


def _guard_socket_connect(*args: object, **kwargs: object) -> None:
    raise RuntimeError(
        "Network access is forbidden in unit/scripts tests (bug 1c68). "
        "Mock the network call (e.g. unittest.mock.patch('urllib.request.urlopen')) "
        "or add @pytest.mark.allow_network if this test genuinely needs network access."
    )


@pytest.fixture(autouse=True)
def _dso_network_guard(request: pytest.FixtureRequest) -> Iterator[None]:
    """Block real socket connections in unit and scripts test tiers (bug 1c68).

    Patches ``socket.socket.connect`` and ``socket.create_connection`` to raise
    ``RuntimeError`` for every test whose path falls under tests/unit/ or
    tests/scripts/, unless the test is decorated with
    ``@pytest.mark.allow_network``.

    Uses stdlib ``unittest.mock.patch`` — no new dependencies (rule:risky-dep).
    """
    if not _in_guarded_tier(request.node):
        yield
        return
    if request.node.get_closest_marker("allow_network"):
        yield
        return

    with (
        patch.object(socket.socket, "connect", _guard_socket_connect),
        patch(
            "socket.create_connection",
            side_effect=RuntimeError(
                "Network access is forbidden in unit/scripts tests (bug 1c68). "
                "Mock the network call or add @pytest.mark.allow_network."
            ),
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _dso_disable_telemetry_during_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Block live telemetry POSTs across the entire pytest session.

    The d2f9 emit wrapper (telemetry_emit_wrapper.emit_event) honours
    DSO_TELEMETRY_DISABLE=1 as a hard no-op switch. Until this fixture
    existed, tests that imported runner.py / arbiter_processor.py and
    reached the emit code paths would Popen the telemetry-emit.sh shim,
    which POSTs to review_telemetry.endpoint_url. While that endpoint
    was the SCP-blocked Lambda Function URL every POST silently 403'd,
    masking the leak. Once endpoint_url was repointed to the API
    Gateway (bypassing the SCP), every unguarded test run started
    polluting s3://dso-telemetry-review-820258254566/<client_id>/<date>/
    with synthetic records.

    Tests that intentionally exercise the wrapper (e.g.
    test_telemetry_emit_wrapper.py, test_telemetry_schema_contract.py)
    already call ``monkeypatch.delenv("DSO_TELEMETRY_DISABLE", raising=
    False)`` per-test; pytest applies the per-test monkeypatch AFTER
    this autouse fixture, so those overrides continue to work
    unchanged.
    """
    monkeypatch.setenv("DSO_TELEMETRY_DISABLE", "1")


@pytest.fixture(autouse=True)
def _dso_dummy_anthropic_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set a dummy ANTHROPIC_API_KEY so tests that exercise dispatch_review
    (or any provider-config-validated path) don't fail with
    ``ConfigError: Missing ANTHROPIC_API_KEY`` when run in a CI job that
    lacks the secret (e.g. Python Skill/Doc Tests, ticket-platform-matrix).

    Without this fixture, every test that reaches the provider-config
    validation step needs its own ``monkeypatch.setenv("ANTHROPIC_API_KEY",
    ...)`` even when the LLM call itself is mocked. Bug f148 PR-A surfaced
    this when its R2 test passed locally (key was set in the shell) but
    failed on CI (no key exposed to the unit-test job).

    Tests that intentionally probe the missing-key path (e.g.
    test_providers_config.py:110) already call
    ``monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)`` per-test;
    pytest applies per-test monkeypatch AFTER autouse fixtures, so those
    overrides continue to work unchanged.

    The dummy key shape (sk-test-…) is non-functional — no real API call
    can succeed with it — so leaking it into a real LLM call (e.g. by
    forgetting to mock litellm) will fail loudly with a 401, not silently
    bill a customer's account.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy-key-for-unit-tests")


@pytest.fixture(autouse=True)
def _no_repo_root_leaks() -> Iterator[None]:
    before = set(os.listdir(_REPO_ROOT))
    try:
        yield
    finally:
        after = set(os.listdir(_REPO_ROOT))
        leaked = after - before
        if not leaked:
            return
        for name in leaked:
            target = _REPO_ROOT / name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                try:
                    target.unlink()
                except OSError:
                    # Cleanup is best-effort — pytest.fail() below already
                    # surfaces the leak. Suppressing keeps a permissions or
                    # races race from masking the real failure.
                    pass
        pytest.fail(
            "Test leaked new entries into REPO_ROOT (use tmp_path or a "
            f"sandboxed temp dir): {sorted(leaked)}"
        )
