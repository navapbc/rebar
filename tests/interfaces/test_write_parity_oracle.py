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
from adapters import TRANSITION_WRITE_PARAMS, CliAdapter, LibraryAdapter, McpAdapter
from write_parity_contract import (
    ACCEPTED,
    ADAPTER_BOUND_INTERNALS,
    CASES,
    PARAM_NOT_EXPOSED,
    REJECTED,
    Case,
    execute,
)

import rebar

_ADAPTERS = {"library": LibraryAdapter, "cli": CliAdapter, "mcp": McpAdapter}


def _case(case_id: str) -> Case:
    return next(c for c in CASES if c.id == case_id)


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

    # Store-invariance: a REJECTED op must have applied NO write — the subject is
    # left at its pre-op status. Guards against a rejection that nonetheless took
    # partial effect (a param applying despite a non-zero exit).
    if result.kind == REJECTED and case.unmutated_status and subject is not None:
        actual = rebar.show_ticket(subject, repo_root=str(rebar_repo))["status"]
        assert actual == case.unmutated_status, (
            f"{case.id} via {adapter_name}: REJECTED but status is {actual!r}, "
            f"expected unchanged {case.unmutated_status!r} — a partial write leaked"
        )


def test_adapter_bound_internals_are_not_contract_params() -> None:
    """Negative control (structural half): the per-surface plumbing params
    (source/return_alias/_creation_channel/repo_root) are NOT write-contract
    params, so they can never register as a false parity divergence. The
    behavioral half is the ``create-baseline`` row — create succeeds identically
    on all three surfaces despite each threading its own internals.
    """
    contract_params = set(TRANSITION_WRITE_PARAMS) | {"assignee"}
    leaked = set(ADAPTER_BOUND_INTERNALS) & contract_params
    assert not leaked, f"adapter-bound internals leaked into the contract: {leaked}"


class _ConvergedMcpAdapter(McpAdapter):
    """The MCP write surface as it will be AFTER ``scratchy-leprous-galago``
    lands: the reason-carrying params are advertised and threaded through to the
    engine. Used only by the convergence test to prove the oracle flips a
    strict-xfail MCP row to an xpass once the gap closes.
    """

    def _tool_params(self, tool: str) -> set[str]:
        return super()._tool_params(tool) | set(TRANSITION_WRITE_PARAMS)

    def transition(self, tid, current, target, **kw):  # type: ignore[override]
        return LibraryAdapter().transition(tid, current, target, **kw)

    def claim(self, tid, assignee=None, **kw):  # type: ignore[override]
        return LibraryAdapter().claim(tid, assignee=assignee, **kw)


def test_oracle_detects_convergence(rebar_repo: Path) -> None:
    """The strict-xfail MCP rows are a genuine forcing function, proven in-suite.

    A row that is PARAM_NOT_EXPOSED on today's MCP surface classifies ACCEPTED on
    a parity-complete (post-scratchy) MCP surface — so when the gap lands the
    strict-xfail xpasses and its marker must be deleted. This asserts both ends
    of that transition against real stores rather than merely recording it.
    """
    case = _case("force-claim")
    assert case.xfail.get("mcp"), "guard: force-claim must be a strict-xfail MCP row today"

    gap, _ = execute(McpAdapter(), case, rebar_repo)
    assert gap.kind == PARAM_NOT_EXPOSED, (
        f"expected today's MCP surface to lack the force param, got {gap}"
    )

    converged, subject = execute(_ConvergedMcpAdapter(), case, rebar_repo)
    assert converged == case.expected, (
        f"parity-complete MCP surface classified {converged}, expected {case.expected} "
        "— the oracle would not detect convergence"
    )
    actual = rebar.show_ticket(subject, repo_root=str(rebar_repo))["status"]
    assert actual == case.expected_status
