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

Also provides a network-escape guard for tests/unit/** and tests/scripts/**.
Any test in those tiers that opens a real socket raises ``RuntimeError`` with a
clear message. Tests that legitimately need network access (none expected in
these tiers) may opt out via ``@pytest.mark.allow_network``.
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
        "Network access is forbidden in unit/scripts tests. "
        "Mock the network call (e.g. unittest.mock.patch('urllib.request.urlopen')) "
        "or add @pytest.mark.allow_network if this test genuinely needs network access."
    )


@pytest.fixture(autouse=True)
def _network_guard(request: pytest.FixtureRequest) -> Iterator[None]:
    """Block real socket connections in unit and scripts test tiers.

    Patches ``socket.socket.connect`` and ``socket.create_connection`` to raise
    ``RuntimeError`` for every test whose path falls under tests/unit/ or
    tests/scripts/, unless the test is decorated with
    ``@pytest.mark.allow_network``.

    Uses stdlib ``unittest.mock.patch`` — no new dependencies.
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
                "Network access is forbidden in unit/scripts tests. "
                "Mock the network call or add @pytest.mark.allow_network."
            ),
        ),
    ):
        yield


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
