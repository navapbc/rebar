"""Ticket aff0: SupportsAbsenceProbe capability + Jira probe adapter (happy path).

The wholly-Jira inbound probe (REST URL, basic-auth GET, status-name classification,
JIRA_* env) moves out of the neutral core into ``adapters/jira/probe.py`` behind a new
``SupportsAbsenceProbe`` capability Protocol. The neutral vocabulary
(ProbeBranch/ProbeResult/ProbeConfigError) stays at root ``inbound_probe.py``.

Happy-path oracle: classification of a live 200 response, the capability presence on
JiraBackend, and the dispatch routing through ``backend.probe_remote`` when the backend
has the capability.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from rebar_reconciler import inbound_probe
from rebar_reconciler._backend import SupportsAbsenceProbe
from rebar_reconciler.adapters.jira import probe as jira_probe

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[4]
_REC = _REPO / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _REC / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Jira adapter classification (happy) ──────────────────────────────────────
def test_classifies_present_resolved() -> None:
    r = jira_probe.classify_probe_response("PROJ-1", 200, {"fields": {"status": {"name": "Done"}}})
    assert r.branch == inbound_probe.ProbeBranch.PRESENT_RESOLVED


def test_classifies_present_filtered() -> None:
    r = jira_probe.classify_probe_response(
        "PROJ-2", 200, {"fields": {"status": {"name": "In Progress"}}}
    )
    assert r.branch == inbound_probe.ProbeBranch.PRESENT_FILTERED


# ── JiraBackend advertises the capability ────────────────────────────────────
def test_jira_backend_supports_absence_probe() -> None:
    from rebar_reconciler.adapters.jira.backend import JiraBackend

    assert isinstance(JiraBackend(transport=object()), SupportsAbsenceProbe)


# ── dispatch routes through backend.probe_remote when capable ────────────────
def _make_probe_mutation(mut_mod, target: str = "PROJ-1"):
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.probe,
        target=target,
        payload={"reason": "absent_partner"},
        provenance={"source": "differ", "local_target": "LOCAL-1"},
    )


def test_dispatch_uses_backend_probe_when_capable() -> None:
    mut_mod = _load("reconcile_mutation", "mutation.py")
    run_differs = _load("run_differs_probe_happy", "run_differs.py")

    class ProbingBackend:
        """A backend that HAS SupportsAbsenceProbe."""

        def __init__(self) -> None:
            self.seen: list[str] = []

        def probe_remote(self, remote_id: str):
            self.seen.append(remote_id)
            return inbound_probe.ProbeResult(
                inbound_probe.ProbeBranch.PRESENT_RESOLVED, remote_id, {"status": "Done"}
            )

    captured: list = []

    def route(mut, result):
        captured.append(result)
        return None

    backend = ProbingBackend()
    muts = [_make_probe_mutation(mut_mod, "PROJ-1")]
    run_differs._run_differs_inbound_probe_dispatch(muts, route, backend)

    assert backend.seen == ["PROJ-1"]  # the backend's probe was used
    assert captured and captured[0].branch == inbound_probe.ProbeBranch.PRESENT_RESOLVED
