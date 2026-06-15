"""RED tests for differ ProvenanceLedger echo suppression."""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "differ.py"
LEDGER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "conflict_resolver.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def differ():
    return _load(DIFFER_PATH, "differ")


@pytest.fixture(scope="module")
def ledger_mod():
    return _load(LEDGER_PATH, "conflict_resolver")


def test_echo_suppressed_for_identical_elements(differ, ledger_mod):
    """Mutations whose target+payload hash matches the ledger's last entry are suppressed."""
    ledger = ledger_mod.ProvenanceLedger()
    # Record what would be emitted next so it becomes an echo
    ledger.record("PROJ-1", "jira", {"summary": "echoed"})
    local_state = {"LOCAL-1": {"local_id": "id-1", "jira_key": "PROJ-1", "summary": "echoed"}}
    jira_state = {"PROJ-1": {"local_id": "id-1", "summary": "echoed"}}
    mutations = differ.compute_mutations(local_state, jira_state, ledger=ledger)
    # Behavioral: the seeded echo must be SUPPRESSED — i.e. absent from the actual
    # compute_mutations OUTPUT (not merely present in the ledger we populated).
    assert "PROJ-1" not in [m.target for m in mutations], (
        f"echo for PROJ-1 was not suppressed; compute_mutations emitted it: {mutations}"
    )


def test_zero_mutations_on_second_pass(differ, ledger_mod):
    """Two-pass run: pass 1 emits mutations + records them; pass 2 on the same state emits zero."""
    ledger = ledger_mod.ProvenanceLedger()
    local_state = {"LOCAL-1": {"local_id": "id-1", "summary": "x"}}
    jira_state = {}
    # Pass 1 — may emit (outbound, create) for LOCAL-1
    pass1 = differ.compute_mutations(local_state, jira_state, ledger=ledger)
    # Simulate "applied" by recording every emitted mutation's payload
    for m in pass1:
        ledger.record(m.target, "local" if "outbound" in m.direction.value else "jira", m.payload)
    # Pass 2 — same state; ledger now has the recorded values
    pass2 = differ.compute_mutations(local_state, jira_state, ledger=ledger)
    # Non-vacuity: pass 1 must have emitted something, else the test proves nothing.
    assert pass1, "pass 1 emitted no mutations — the second-pass assertion would be vacuous"
    # Behavioral: with pass 1 recorded in the ledger, pass 2 emits ZERO (the docstring's
    # promise) — not merely "fewer". Asserts the actual output, not set arithmetic.
    assert pass2 == [], f"pass 2 must emit ZERO after pass 1 was recorded, got: {pass2}"
