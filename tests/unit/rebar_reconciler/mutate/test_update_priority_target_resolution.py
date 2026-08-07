"""[bug c9c6] Happy path: the priority-update REST write targets the CONFIGURED Jira.

``acli_cli_ops.update_priority`` deliberately bypasses ACLI (ACLI has no priority
edit) and writes with a bare ``urllib`` PUT. It therefore spawns no subprocess, so
an ACLI argv prefix (``acli_cmd``) is structurally inapplicable to it and it no
longer accepts one; it resolves its Jira target through
``acli_subprocess.resolve_jira_settings`` instead.

These tests assert the OBSERVABLE outbound request — the URL, method, body and
Basic-auth credential actually sent — for the normal successful path, both when
``update_priority`` is called directly and when it is reached through the caller
that used to forward ``acli_cmd`` (``acli.update_issue(..., priority=...)``, which
still carries an ``acli_cmd`` for its own ACLI calls).
"""

from __future__ import annotations

import base64
import json
import urllib.request
from typing import Any

import pytest

# Engine is on sys.path via the package conftest.
from rebar_reconciler.adapters.jira import acli as acli_mod
from rebar_reconciler.adapters.jira import acli_cli_ops, acli_subprocess

_URL = "https://configured.atlassian.net"
_USER = "svc@example.com"
_TOKEN = "configured-token"


@pytest.fixture
def sent_requests(monkeypatch: pytest.MonkeyPatch) -> list[urllib.request.Request]:
    """Pin the configured Jira target and capture every bare-urlopen request."""
    monkeypatch.setattr(
        acli_subprocess,
        "resolve_jira_settings",
        lambda **_k: acli_subprocess.JiraSettings(
            url=_URL, user=_USER, project="DIG", api_token=_TOKEN
        ),
    )
    captured: list[urllib.request.Request] = []

    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_exc: Any) -> bool:
            return False

        def read(self) -> bytes:
            return b""  # Jira returns 204 No Content on a successful field PUT.

    def _fake_urlopen(req: urllib.request.Request, *_a: Any, **_k: Any) -> _Resp:
        captured.append(req)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    return captured


def _assert_priority_put(req: urllib.request.Request, key: str, name: str) -> None:
    assert req.get_method() == "PUT"
    assert req.full_url == f"{_URL}/rest/api/3/issue/{key}"
    assert json.loads(bytes(req.data or b"")) == {"fields": {"priority": {"name": name}}}
    expected = base64.b64encode(f"{_USER}:{_TOKEN}".encode()).decode()
    assert req.get_header("Authorization") == f"Basic {expected}"


def test_update_priority_puts_to_the_configured_target(
    sent_requests: list[urllib.request.Request],
) -> None:
    """The direct call issues exactly one PUT, to the configured site and credential."""
    acli_cli_ops.update_priority("DIG-1", "High")

    assert len(sent_requests) == 1
    _assert_priority_put(sent_requests[0], "DIG-1", "High")


def test_update_issue_routes_priority_to_the_configured_target(
    sent_requests: list[urllib.request.Request],
) -> None:
    """Reached through ``update_issue`` — whose ``acli_cmd`` is for its ACLI calls
    only — the priority write still lands on the configured target, unchanged."""
    result = acli_mod.update_issue("DIG-2", priority="High", acli_cmd=["/opt/bin/acli"])

    assert result == {"key": "DIG-2"}
    assert len(sent_requests) == 1
    _assert_priority_put(sent_requests[0], "DIG-2", "High")
