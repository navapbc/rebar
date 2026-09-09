"""Held-out contract for priority target selection and ACLI command scope.

``update_priority`` sends REST directly to the configured Jira target and accepts no
``acli_cmd``. Operations that spawn ACLI retain ``acli_cmd`` as an argument-vector
prefix. Coverage guards both boundaries.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from rebar_reconciler.adapters.jira import acli_cli_ops, acli_subprocess

# ── the parameter is gone from the contract ─────────────────────────────────


def test_update_priority_rejects_acli_cmd() -> None:
    """Not merely ignored -- refused. An inapplicable knob must not be silently
    accepted, which is the false-promise shape the ticket is about."""
    with pytest.raises(TypeError):
        acli_cli_ops.update_priority("DIG-1", "High", acli_cmd=["acli"])  # type: ignore[call-arg]


def test_update_priority_signature_has_no_acli_cmd() -> None:
    assert "acli_cmd" not in inspect.signature(acli_cli_ops.update_priority).parameters


# ── the write still lands on the CONFIGURED target ──────────────────────────


class _Resp:
    status = 204

    def read(self) -> bytes:
        return b""

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        return None


def test_priority_put_targets_the_configured_jira_not_a_hardcoded_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the parameter must not disturb where the write goes."""
    settings = type(
        "S", (), {"url": "https://configured.example.net", "user": "u@x", "api_token": "tok"}
    )()
    monkeypatch.setattr(acli_subprocess, "resolve_jira_settings", lambda: settings)

    seen: dict[str, Any] = {}

    def _fake_urlopen(req: Any, timeout: int = 10) -> _Resp:
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["body"] = req.data
        return _Resp()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    acli_cli_ops.update_priority("DIG-1", "High")

    assert seen["url"] == "https://configured.example.net/rest/api/3/issue/DIG-1"
    assert seen["method"] == "PUT"
    assert b'"priority"' in seen["body"] and b"High" in seen["body"]


# ── the removal did not over-reach ──────────────────────────────────────────


def test_acli_cmd_is_still_honoured_where_a_subprocess_is_actually_spawned() -> None:
    """Subprocess-backed operations retain the acli_cmd argument-vector prefix."""
    assert "acli_cmd" in inspect.signature(acli_subprocess._run_acli).parameters
    for fn_name in ("get_issue", "add_comment"):
        fn = getattr(acli_cli_ops, fn_name)
        assert "acli_cmd" in inspect.signature(fn).parameters, (
            f"{fn_name} spawns acli and must keep its argv-prefix parameter"
        )


def test_run_acli_uses_acli_cmd_as_an_argv_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ACLI prefix precedes its subcommand and carries no Jira target."""
    captured: dict[str, Any] = {}

    class _FakePopen:
        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            captured["cmd"] = list(cmd)
            self.returncode = 0

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "{}", ""

        # `subprocess.run` (used by unrelated fixtures during teardown) enters Popen as
        # a context manager, so the fake must support the protocol or teardown errors.
        def __enter__(self) -> _FakePopen:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(acli_subprocess.subprocess, "Popen", _FakePopen)

    acli_subprocess._run_acli(["jira", "issue", "view"], acli_cmd=["/opt/bin/acli", "--flag"])
    monkeypatch.undo()

    assert captured["cmd"] == ["/opt/bin/acli", "--flag", "jira", "issue", "view"], captured
