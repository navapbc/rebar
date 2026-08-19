"""Write-op parity conformance oracle (ticket topaz-blubbery-mice).

A Pattern-B behavioral oracle: the single contract table in
``write_parity_contract`` is executed through EVERY adapter (library / CLI /
MCP) against a fresh store, and each adapter's classification
(``ACCEPTED`` / ``REJECTED(code)`` / ``PARAM_NOT_EXPOSED``) is asserted to match
the row's transport-agnostic expectation. Because all three are checked against
the same expectation, any surface drift — a param present on one adapter but
missing on another, or a runtime rule enforced inconsistently — fails the suite
WITHOUT an LLM, a change-detector, or a hand-maintained NxM matrix.

Known, ticketed divergences (the MCP force/reason/caused_by/ref gaps tracked by
``scratchy-leprous-galago``) are recorded as per-row strict-xfails: the suite is
GREEN today, and when the gap is closed the MCP classification flips to match
the expectation, the strict-xfail xpasses, and the marker must be deleted — the
intended forcing function for cleanup.

The suite runs as an ordinary pytest with no CI-provider-specific trigger
(portability), reusing the shared three-adapter harness (``adapters``) and the
interfaces sandbox-store bootstrap (``conftest.rebar_repo``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from adapters import CliAdapter, LibraryAdapter, McpAdapter
from write_parity_contract import ACCEPTED, CASES, Case, execute

import rebar

_ADAPTERS = {"library": LibraryAdapter, "cli": CliAdapter, "mcp": McpAdapter}


def _params() -> list:
    """One parametrization per (case × adapter), strict-xfailing known gaps."""
    out = []
    for case in CASES:
        for name in _ADAPTERS:
            marks = []
            if name in case.xfail:
                marks.append(
                    pytest.mark.xfail(
                        strict=True,
                        reason=(
                            f"{name} does not yet expose this write param; tracked by "
                            f"{case.xfail[name]}. When it lands, this xpasses — delete the marker."
                        ),
                    )
                )
            out.append(pytest.param(case, name, id=f"{case.id}-{name}", marks=marks))
    return out


@pytest.mark.parametrize("case,adapter_name", _params())
def test_write_parity(case: Case, adapter_name: str, rebar_repo: Path) -> None:
    adapter = _ADAPTERS[adapter_name]()
    result, subject = execute(adapter, case, rebar_repo)

    assert result == case.expected, (
        f"{case.id} via {adapter_name}: classified {result}, expected {case.expected} "
        "— write-surface parity divergence"
    )

    # Effect: an ACCEPTED op must actually have moved the ticket, so a silently
    # dropped param (e.g. a flag the CLI parser ignores) cannot pass as ACCEPTED.
    if result.kind == ACCEPTED and case.expected_status and subject is not None:
        actual = rebar.show_ticket(subject, repo_root=str(rebar_repo))["status"]
        assert actual == case.expected_status, (
            f"{case.id} via {adapter_name}: accepted but status is {actual!r}, "
            f"expected {case.expected_status!r} — the param did not take effect"
        )
