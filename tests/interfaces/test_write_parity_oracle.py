"""Transport-neutral write parity oracle (ticket topaz-blubbery-mice).

Each contract row runs through library, CLI, and MCP against a fresh store and
must match one shared ``ACCEPTED`` / ``REJECTED(code)`` /
``PARAM_NOT_EXPOSED`` expectation. A populated strict xfail marks a ticketed gap
and fails on convergence. All current maps are empty. This portable pytest suite
reuses the shared adapter and interface-store harnesses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from adapters import TRANSITION_WRITE_PARAMS, CliAdapter, LibraryAdapter, McpAdapter
from write_parity_contract import (
    ACCEPTED,
    ADAPTER_BOUND_INTERNALS,
    CASES,
    REJECTED,
    Case,
    execute,
)

import rebar

_ADAPTERS = {"library": LibraryAdapter, "cli": CliAdapter, "mcp": McpAdapter}


def _case(case_id: str) -> Case:
    return next(c for c in CASES if c.id == case_id)


def _params() -> list:
    """Parametrize case/adapter pairs. Declare strict-xfail gaps."""
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


@pytest.mark.parametrize(("case", "adapter_name"), _params())
def test_write_parity(case: Case, adapter_name: str, rebar_repo: Path) -> None:
    adapter = _ADAPTERS[adapter_name]()
    result, subject = execute(adapter, case, rebar_repo)

    assert result == case.expected, (
        f"{case.id} via {adapter_name}: classified {result}, expected {case.expected} "
        "— write-surface parity divergence"
    )

    # An accepted result must move the ticket, catching silently dropped parameters.
    if result.kind == ACCEPTED and case.expected_status and subject is not None:
        actual = rebar.show_ticket(subject, repo_root=str(rebar_repo))["status"]
        assert actual == case.expected_status, (
            f"{case.id} via {adapter_name}: accepted but status is {actual!r}, "
            f"expected {case.expected_status!r} — the param did not take effect"
        )

    # A rejected result must leave the subject unchanged, excluding partial writes.
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


def test_oracle_detects_convergence(rebar_repo: Path) -> None:
    """A converged MCP row stays unmarked and executes through the real adapter."""
    case = _case("force-claim")
    assert "mcp" not in case.xfail, "guard: converged force-claim must not remain xfailed"

    converged, subject = execute(McpAdapter(), case, rebar_repo)
    assert converged == case.expected, (
        f"parity-complete MCP surface classified {converged}, expected {case.expected} "
        "— the oracle would not detect convergence"
    )
    actual = rebar.show_ticket(subject, repo_root=str(rebar_repo))["status"]
    assert actual == case.expected_status
