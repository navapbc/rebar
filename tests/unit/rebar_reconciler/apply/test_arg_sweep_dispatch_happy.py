"""HAPPY-path oracle for the ARG lint sweep (ticket fc0c-e218-8b34-4858).

The sweep normalizes the ten reconciler leaf signatures (Cluster-1) so their
accepted-but-unused parameters are absorbed by ``**_kwargs`` instead of tripping
ruff's ARG rule. The behavioural contract that MUST survive that edit is that
``applier._apply_typed`` still routes each ``(direction, action)`` mutation to the
correct leaf and the leaf still reaches the transport — i.e. the signature
normalization is behaviour-preserving for dispatch.

This file is the happy-path specification handed to the implementer: it pins the
routing invariant through the REAL dispatcher (``_apply_typed``, whose runtime
signature introspection decides which of ``repo_root`` / ``binding_store`` to pass)
and pins that the two Cluster-1 leaf modules are ARG-clean after the sweep. The
discriminating edge cases (the inertness of the newly-delivered ``binding_store`` /
``repo_root`` keywords, and the Cluster-2 Protocol keyword-conformance) are held
out of the implementer's tree.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


def _load_applier():
    spec = importlib.util.spec_from_file_location("arg_sweep_happy_applier", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["arg_sweep_happy_applier"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


def _probe_mutation(applier):
    mut_mod = applier._load_mutation_module()
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.outbound,
        action=mut_mod.MutationAction.probe,
        target="MOCK-1",
        payload={},
        provenance={"source": "arg-sweep-oracle"},
    )


def test_apply_typed_routes_outbound_probe_to_its_leaf(applier):
    """Dispatch routing survives the signature normalization.

    Drives the real ``_apply_typed`` (its runtime signature introspection is the
    thing the sweep perturbs) for an outbound probe and asserts the OBSERVABLE
    result: the probe verdict derived from the live client response, the result
    tagged with the right direction/action, and the transport actually reached
    with the target key. A leaf whose normalization dropped a parameter it reads
    (``client``) or broke dispatch would fail here.
    """
    mut_mod = applier._load_mutation_module()
    issue = {"key": "MOCK-1", "fields": {"summary": "hello"}}
    client = MagicMock()
    client.get_issue = MagicMock(return_value=issue)

    result = applier._apply_typed(_probe_mutation(applier), client=client)

    assert result.direction is mut_mod.MutationDirection.outbound
    assert result.action is mut_mod.MutationAction.probe
    assert result.payload == {"present": True, "issue": issue}
    client.get_issue.assert_called_once_with("MOCK-1")


@pytest.mark.parametrize(
    "rel_path",
    [
        "src/rebar/_engine/rebar_reconciler/apply_inbound.py",
        "src/rebar/_engine/rebar_reconciler/apply_outbound.py",
    ],
)
def test_cluster1_leaf_module_is_arg_clean(rel_path):
    """The two Cluster-1 leaf modules carry ZERO residual ARG violations.

    This is the sweep's definition-of-done for the behaviourally-risky files:
    after ``**_kwargs`` normalization ``ruff check --select ARG`` must report
    nothing for them. Scoped to these files (not the whole tree) so the assertion
    is immune to tracked-defect residuals elsewhere while still failing if any
    leaf in these modules was missed.
    """
    target = REPO_ROOT / rel_path
    proc = subprocess.run(
        [
            "ruff",
            "check",
            "--no-cache",
            "--select",
            "ARG",
            "--output-format",
            "concise",
            str(target),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    combined = proc.stdout + proc.stderr
    arg_lines = [ln for ln in combined.splitlines() if ":" in ln and " ARG" in ln]
    assert arg_lines == [], f"residual ARG violations in {rel_path}:\n" + "\n".join(arg_lines)
