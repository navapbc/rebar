"""Unit coverage for the live-DC harness's canonical bridge-route output parsing.

The cells that consume these predicates run only against live Jira DC in CI's external
lane. This module pins their parsing in the ordinary unit lane — no Jira, no network, no
credentials, no ``rebar_reconciler`` import — so a change to the reconciler's stream
contract is caught by a local ``pytest`` run instead of by a 40-minute scheduled live job.

The module under test is loaded by path because it lives under ``tests/external/`` and is
not importable as a package from here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "external" / "live_jira_dc" / "_bridge_output.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_bridge_output_under_test", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge_output = _load()


# --- captures -------------------------------------------------------------------------
#
# Shaped after real reconciler output. stdout for a plain `sync` carries the invariants
# line (and no JSON envelope, since sync is not a no_write route); the disposition line and
# the RECON telemetry are on stderr.

_STDOUT_SYNC = "invariants: scanned=1 filed=0 (cap=5)\n"

_STDERR_CONVERGED_NOOP = (
    "RECON: outbound_differ total=0 create=0 update=0 delete=0\n"
    "RECON: inbound_differ total=0 with_fields=0 with_labels=0 with_comments=0\n"
    "RECON: bidir_suppressed inbound=0\n"
    "BRIDGE_STATE: converged\n"
)

_STDERR_CONVERGED_WROTE = (
    "RECON: outbound_differ total=2 create=1 update=1 delete=0\n"
    "RECON: inbound_differ total=0 with_fields=0 with_labels=0 with_comments=0\n"
    "RECON: batch_outcome action=create key=RBJ-1 error=None\n"
    "RECON: batch_outcome action=update key=RBJ-2 error=None links_applied=1\n"
    "BRIDGE_STATE: converged\n"
)


def test_a_converged_noop_sync_pass_satisfies_both_predicates() -> None:
    assert bridge_output.converged_pass_problem(_STDOUT_SYNC, _STDERR_CONVERGED_NOOP) is None
    assert bridge_output.wrote_nothing_problem(_STDOUT_SYNC, _STDERR_CONVERGED_NOOP) is None


def test_a_writing_pass_is_converged_but_is_not_a_noop() -> None:
    """The case a one-for-one BRIDGE_STATE substitution would have silently missed.

    A pass that applied mutations still reports CONVERGED, so the idempotence predicate must
    reject it on the applied-mutation evidence rather than on the disposition line.
    """
    assert bridge_output.converged_pass_problem(_STDOUT_SYNC, _STDERR_CONVERGED_WROTE) is None

    problem = bridge_output.wrote_nothing_problem(_STDOUT_SYNC, _STDERR_CONVERGED_WROTE)
    assert problem is not None
    assert "applied 2 mutation" in problem
    assert bridge_output.applied_mutation_count(_STDERR_CONVERGED_WROTE) == 2


@pytest.mark.parametrize(
    ("outbound", "inbound"),
    [(3, 0), (0, 2), (3, 2)],
    ids=["outbound-only", "inbound-only", "both"],
)
def test_nonzero_differ_totals_fail_the_noop_predicate(outbound: int, inbound: int) -> None:
    """Work computed but not yet applied is still a failed convergence."""
    stderr = (
        f"RECON: outbound_differ total={outbound} create={outbound} update=0 delete=0\n"
        f"RECON: inbound_differ total={inbound} with_fields={inbound} "
        "with_labels=0 with_comments=0\n"
        "BRIDGE_STATE: converged\n"
    )
    problem = bridge_output.wrote_nothing_problem(_STDOUT_SYNC, stderr)
    assert problem is not None
    assert "differ computed work" in problem


@pytest.mark.parametrize(
    "stderr",
    [
        "RECON: outbound_differ total=0 create=0 update=0 delete=0\n",
        "BRIDGE_STATE: in-flight\n",
        "BRIDGE_STATE: reschedule\n",
    ],
    ids=["no-disposition-line", "in-flight", "reschedule"],
)
def test_a_pass_that_did_not_converge_fails_both_predicates(stderr: str) -> None:
    assert bridge_output.converged_pass_problem(_STDOUT_SYNC, stderr) is not None
    assert bridge_output.wrote_nothing_problem(_STDOUT_SYNC, stderr) is not None


def test_the_legacy_ok_line_does_not_satisfy_the_canonical_predicates() -> None:
    """Regression guard for the defect this fix repairs, pinned from the other direction.

    A compatibility ``--mode`` capture proves its success with an ``OK:`` line on stdout and
    emits no ``BRIDGE_STATE:`` line at all. The canonical predicates must reject it — and
    say why — so a future re-point at the wrong route fails loudly instead of silently.
    """
    stdout = "OK: steady-state pass converged — 0 mutations\n"
    stderr = "RECON: outbound_differ total=0 create=0 update=0 delete=0\n"

    problem = bridge_output.converged_pass_problem(stdout, stderr)
    assert problem is not None
    assert "LEGACY" in problem
    assert bridge_output.wrote_nothing_problem(stdout, stderr) is not None


def test_empty_output_fails_both_predicates_naming_the_missing_signal() -> None:
    assert "BRIDGE_STATE: converged" in str(bridge_output.converged_pass_problem("", ""))
    assert "BRIDGE_STATE: converged" in str(bridge_output.wrote_nothing_problem("", ""))


def test_a_converged_pass_that_never_reached_the_differ_cannot_claim_zero_writes() -> None:
    """Absent telemetry is not evidence of absence — refuse to certify the no-op."""
    problem = bridge_output.wrote_nothing_problem(_STDOUT_SYNC, "BRIDGE_STATE: converged\n")
    assert problem is not None
    assert "did not reach the differ stage" in problem
