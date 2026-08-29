"""STEP_CREATE must type a missing transport binary as ``transport_unavailable`` (bug ac93-8afd).

When the ``acli`` transport binary is absent, ``subprocess.Popen(["acli", ...])`` raises
``FileNotFoundError``. The six-step probe (``run_access_check``) used to catch that in its broad
``except Exception`` catch-all and surface it verbatim as
``reason=exception, detail="[Errno 2] No such file or directory: 'acli'"`` — a raw, untyped leak
of an infrastructure fault into the probe verdict. ``access_check`` already types an unreachable
transport elsewhere (``TransportUnavailableError`` / ``ProjectVisibilityResult.status ==
"transport_unavailable"``); the STEP_CREATE probe must map a missing-transport binary to that same
typed vocabulary instead of the raw errno string. The verdict/return-code failure signal is
unchanged — only the leaked exception is wrapped into a typed ``reason``.
"""

from __future__ import annotations

import pytest

from rebar._lib_ops import _engine_module

pytestmark = pytest.mark.unit

_PROBE_ENV = {
    "JIRA_URL": "https://example.atlassian.net",
    "JIRA_USER": "operator@example.com",
    "JIRA_API_TOKEN": "secret",
    "JIRA_PROJECT": "DIG",
}


class _MissingAcliClient:
    """A client whose create_issue fails exactly as a missing ``acli`` binary does."""

    def __init__(self, **_kwargs) -> None:
        pass

    def create_issue(self, _fields):
        # This is what subprocess.Popen(["acli", ...]) raises when acli is absent/unresolvable.
        raise FileNotFoundError(2, "No such file or directory", "acli")

    def delete_issue(self, _key):  # pragma: no cover - never reached (create failed first)
        return None


class _GenericFailureClient:
    """A client whose create_issue fails with an ordinary provider error (NOT transport-missing)."""

    def __init__(self, **_kwargs) -> None:
        pass

    def create_issue(self, _fields):
        raise RuntimeError("jira said no")

    def delete_issue(self, _key):  # pragma: no cover - never reached (create failed first)
        return None


def test_missing_transport_binary_is_typed_transport_unavailable() -> None:
    """A missing ``acli`` binary at STEP_CREATE yields a typed transport_unavailable verdict."""
    access_check = _engine_module("rebar_reconciler.access_check")

    result, lines, returncode = access_check.run_access_check(
        env=_PROBE_ENV, client_cls=_MissingAcliClient
    )

    # The failure signal itself is UNCHANGED (still a probe failure, exit 1).
    assert result["verdict"] == "FAIL"
    assert returncode == 1

    step = result["steps"][0]
    assert step["step"] == "STEP_CREATE"
    assert step["passed"] is False
    # The typed reason replaces the raw ``exception`` catch-all reason ...
    assert step["reason"] == "transport_unavailable"
    # ... and the raw errno string must NOT leak into the detail.
    assert "No such file or directory" not in str(step.get("detail", ""))
    assert "transport_unavailable" in " ".join(lines)
    assert not any("No such file or directory" in line for line in lines)


def test_a_generic_provider_error_is_still_reported_as_exception() -> None:
    """The fix is narrow: an ordinary provider failure keeps the ``exception`` reason."""
    access_check = _engine_module("rebar_reconciler.access_check")

    result, _lines, returncode = access_check.run_access_check(
        env=_PROBE_ENV, client_cls=_GenericFailureClient
    )

    assert result["verdict"] == "FAIL"
    assert returncode == 1
    step = result["steps"][0]
    assert step["step"] == "STEP_CREATE"
    assert step["reason"] == "exception"
