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


# ── the dispatch consumer is GONE (bug 3b5f); the capability stays dormant ────
# ``_run_differs_inbound_probe_dispatch`` was removed with its only producer
# (``differ._compute_mutations_emit_absent_partner_probes``, which could never fire
# from the real call site). ``probe_remote`` / ``SupportsAbsenceProbe`` /
# ``inbound_probe.py`` are deliberately KEPT as a dormant port: removing DC's
# ``probe_remote`` would drop DC's only import of the shared
# ``classify_probe_response``, leaving it Cloud-only and weakening epic e369's AC5
# (one classifier implementation imported by BOTH backends). The cells above still
# pin the classifier and the capability advertisement; only the dead dispatch cell
# is gone.
