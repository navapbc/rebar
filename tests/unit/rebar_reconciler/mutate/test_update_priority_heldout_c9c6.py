"""Held-out validation for bug c9c6 — authored independently of the implementation.

The ticket's open question was whether ``acli_cmd`` can encode a Jira target distinct
from ``resolve_jira_settings()``. It cannot: it is consumed in exactly ONE place,
``acli_subprocess._run_acli`` (``base = acli_cmd if acli_cmd is not None else
_DEFAULT_ACLI_CMD; full_cmd = base + cmd``), i.e. an argv prefix for the ``acli``
binary. ``update_priority`` spawns nothing at all -- it is a direct ``urllib`` PUT --
so no argv prefix could ever apply to it. Signature hygiene, not a cross-target write.

These tests pin the three things that must hold after the parameter is removed, and
in particular the one the fix could plausibly get wrong: over-reaching and stripping
``acli_cmd`` from the paths that legitimately need it.
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
    """The regression this fix could plausibly cause: stripping `acli_cmd` from the
    paths that DO exec the binary. `_run_acli` must still accept it, and the sibling
    ops that spawn acli must still take it."""
    assert "acli_cmd" in inspect.signature(acli_subprocess._run_acli).parameters
    for fn_name in ("get_issue", "add_comment"):
        fn = getattr(acli_cli_ops, fn_name)
        assert "acli_cmd" in inspect.signature(fn).parameters, (
            f"{fn_name} spawns acli and must keep its argv-prefix parameter"
        )


def test_run_acli_uses_acli_cmd_as_an_argv_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the semantics the investigation rests on: `acli_cmd` is prepended to the
    subcommand argv and carries no target/credential of its own. If this ever stopped
    being true, the (b) answer -- and therefore this whole remediation -- would need
    revisiting."""
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
