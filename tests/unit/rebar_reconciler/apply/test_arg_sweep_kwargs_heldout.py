"""HELD-OUT oracle for the ARG lint sweep (ticket fc0c-e218-8b34-4858).

These are the discriminating cases withheld from the implementer's working tree.
They separate a careful, behaviour-preserving sweep from a sloppy one that either
makes the newly-delivered ``binding_store`` / ``repo_root`` keywords observable
(they must stay inert), or that RENAMES a Cluster-2 Protocol parameter to silence
ruff (which would break the keyword call sites and the Protocol contract) instead
of suppressing it with ``# noqa: ARG002``.

Two clusters are pinned, both through REAL entry points asserting OBSERVABLE output:

* Cluster-1 (``**_kwargs`` normalization) — normalizing the ten leaves flips
  ``_apply_typed``'s ``accepts_repo_root`` / ``accepts_binding_store`` introspection
  to True for every leaf, so a leaf that previously did NOT receive those keywords
  now does. The contract is that the extra keyword is INERT: the observable result
  is byte-for-byte identical whether or not a value is supplied.
* Cluster-2 (Protocol methods) — the shared ``OutboundFieldMapper`` and the Data
  Center backend accept-and-ignore several Protocol-mandated keyword parameters.
  The sweep must NOT rename them (real callers in ``outbound_field_diff`` /
  ``outbound_differ`` pass them BY KEYWORD, and the ``OutboundMapper`` Protocol
  pins the names) and must NOT ``# noqa: ARG`` them either: ``ARG`` is not in the
  enabled rule ``select`` while ``RUF100`` is, so an ARG noqa is an unused-noqa
  that fails default ``ruff check``. They therefore stay as raw ``--select ARG``
  residuals (the irreducible floor) while the params keep their Protocol names.
  Renaming ``binding_store`` -> ``_binding_store`` would raise ``TypeError`` at the
  keyword call sites and silently drop the Protocol conformance.
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
    spec = importlib.util.spec_from_file_location("arg_sweep_heldout_applier", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["arg_sweep_heldout_applier"] = mod
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


def _run_probe(applier, **extra_kwargs):
    """Dispatch an outbound probe through the real ``_apply_typed`` and return
    (result, client) — the probe leaf is read-only so it is a clean witness."""
    issue = {"key": "MOCK-1", "fields": {"summary": "hi"}}
    client = MagicMock()
    client.get_issue = MagicMock(return_value=issue)
    result = applier._apply_typed(_probe_mutation(applier), client=client, **extra_kwargs)
    return result, client


# ---------------------------------------------------------------------------
# Cluster-1: newly-delivered keywords are inert
# ---------------------------------------------------------------------------


def test_binding_store_keyword_is_inert_for_dispatch(applier):
    """Supplying ``binding_store`` produces an identical observable result.

    After normalization ``_apply_typed`` delivers ``binding_store`` to a leaf that
    formerly never saw it. This proves that delivery is behaviour-preserving: the
    probe verdict and the transport interaction are identical whether the keyword
    is ``None`` or a distinct sentinel. (Teeth: making a leaf branch on
    ``binding_store`` diverges the two payloads and fails this.)
    """
    baseline, c0 = _run_probe(applier, binding_store=None)
    with_store, c1 = _run_probe(applier, binding_store=object())

    assert with_store.payload == baseline.payload
    assert with_store.direction is baseline.direction
    assert with_store.action is baseline.action
    c0.get_issue.assert_called_once_with("MOCK-1")
    c1.get_issue.assert_called_once_with("MOCK-1")


def test_repo_root_keyword_is_inert_for_a_leaf_that_ignores_it(applier):
    """Supplying ``repo_root`` to a leaf that does not consume it is inert.

    The outbound probe carries an accepted-but-unused ``repo_root`` (one of the
    swept ARG sites); after ``**_kwargs`` normalization it is absorbed. The result
    must be identical whether ``repo_root`` is ``None`` or a real path.
    """
    baseline, _ = _run_probe(applier, repo_root=None)
    with_root, _ = _run_probe(applier, repo_root=REPO_ROOT)

    assert with_root.payload == baseline.payload
    assert with_root.direction is baseline.direction
    assert with_root.action is baseline.action


# ---------------------------------------------------------------------------
# Cluster-2: Protocol keyword parameters must be noqa'd, not renamed
# ---------------------------------------------------------------------------


def _dc_backend():
    import importlib.util

    from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend

    support_path = Path(__file__).resolve().parents[1] / "backend_support.py"
    spec = importlib.util.spec_from_file_location("arg_sweep_backend_support", support_path)
    assert spec is not None and spec.loader is not None
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)  # type: ignore[union-attr]
    return JiraDataCenterBackend(transport=support.FakeTransport())


def test_map_fields_to_remote_accepts_every_protocol_keyword(applier):
    """``map_fields_to_remote`` is still callable with all its Protocol keywords.

    Real callers (``outbound_field_diff``) invoke this with ``ticket=``,
    ``binding_store=`` and ``local_ticket_types=`` BY KEYWORD. If the sweep renamed
    any of those parameters to silence ARG, this call raises ``TypeError``. We
    assert both callability by keyword AND the observable mapping (``title`` ->
    ``summary``), so a rename cannot pass by accident.
    """
    del applier  # only used to force the reconciler import path via the fixture chain
    backend = _dc_backend()
    out = backend.outbound.map_fields_to_remote(
        {"title": "Renamed"},
        ticket=None,
        binding_store=None,
        local_ticket_types=None,
    )
    assert out == {"summary": "Renamed"}


def test_map_local_to_remote_accepts_every_protocol_keyword(applier):
    """``map_local_to_remote`` is still callable with all its Protocol keywords.

    ``outbound_differ`` passes ``binding_store=``, ``local_ticket_types=`` and
    ``suppressed_out=`` by keyword. A rename of any of them would break that caller;
    this pins the keyword contract and the observable mapped result.
    """
    del applier
    backend = _dc_backend()
    ticket = {"local_id": "L1", "title": "Hello", "type": "task"}
    suppressed: list[str] = []
    out = backend.outbound.map_local_to_remote(
        ticket,
        binding_store=None,
        local_ticket_types=None,
        emit_detach_clear=False,
        suppressed_out=suppressed,
    )
    assert out.get("summary") == "Hello"


@pytest.mark.parametrize(
    "rel_path",
    [
        "src/rebar/_engine/rebar_reconciler/adapters/jira_family/outbound_mapper.py",
        "src/rebar/_engine/rebar_reconciler/adapters/jira_datacenter/backend.py",
        "src/rebar/_engine/rebar_reconciler/apply_inbound.py",
        "src/rebar/_engine/rebar_reconciler/apply_outbound.py",
    ],
)
def test_swept_module_passes_default_ruff(rel_path):
    """Every swept module passes the REAL CI lint gate (default ``ruff check``).

    This is the invariant that actually gates the change — and the trap the sweep
    must avoid. ``ARG`` is deliberately NOT in ``pyproject`` ``select`` (this ticket
    must not add it), but ``RUF`` (hence ``RUF100``, unused-noqa) IS. So a Cluster-2
    parameter cannot be silenced with ``# noqa: ARG002``: that directive references a
    non-enabled rule, which ``RUF100`` flags as unused, failing default ``ruff check``
    and the pre-commit hook. A keyword-pinned param that can neither be ``_``-renamed
    (breaks callers) nor ``# noqa``'d must therefore stay as a RAW ``--select ARG``
    residual with no directive. This test fails if the sweep introduced any such
    unused ARG noqa (RUF100) or any other default-select violation.
    """
    target = REPO_ROOT / rel_path
    proc = subprocess.run(
        ["ruff", "check", "--no-cache", "--output-format", "concise", str(target)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    combined = proc.stdout + proc.stderr
    err_lines = [ln for ln in combined.splitlines() if ":" in ln and "Unused `noqa`" in ln]
    assert proc.returncode == 0, f"default ruff check failed for {rel_path}:\n{combined}"
    assert err_lines == [], f"unused-noqa (RUF100) in {rel_path}:\n" + "\n".join(err_lines)


def test_arg_backlog_is_substantially_reduced():
    """The ``--select ARG`` audit backlog is driven down to its irreducible floor.

    Drift-tolerant regression guard: the sweep took the backlog from 64 to the
    keyword-pinned/tracked-defect irreducible set. We assert a strict reduction
    well below the pre-sweep count (a bare ``_``-rename of the reducible buckets is
    the bulk of the win) without pinning an exact number that legitimately drifts as
    tracked defects are fixed elsewhere. Cluster-1's ten leaves in particular must
    be gone (proven ARG-clean by the happy oracle); this pins the whole-tree total.
    """
    proc = subprocess.run(
        [
            "ruff",
            "check",
            "--no-cache",
            "--select",
            "ARG",
            "--output-format",
            "concise",
            "src/rebar",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    combined = proc.stdout + proc.stderr
    arg_lines = [ln for ln in combined.splitlines() if ":" in ln and " ARG" in ln]
    # Pre-sweep was 64; the reducible buckets (Cluster-1 **_kwargs + safe renames)
    # are the large majority, so the residual must be a small keyword-pinned floor.
    assert len(arg_lines) <= 25, (
        f"ARG backlog not reduced enough ({len(arg_lines)}):\n" + "\n".join(arg_lines)
    )
    # Cluster-1 leaf files must be fully clean (normalized to **_kwargs).
    cluster1 = [ln for ln in arg_lines if "apply_inbound.py" in ln or "apply_outbound.py" in ln]
    assert cluster1 == [], "Cluster-1 leaves not fully swept:\n" + "\n".join(cluster1)
