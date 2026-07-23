"""Ticket aff0 (HELD-OUT edge oracle): probe adapter edges + capability degradation.

Withheld from the implementer: the branches that separate a real probe port from a
happy-path fake — the 4xx/5xx classification edges, the GET-only invariant, the
missing-env error, the capability-LACKING → UNREACHABLE degradation, and the proof the
neutral vocabulary stayed at root with no vendor import.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

from rebar_reconciler import inbound_probe
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


# ── adapter classification edges ─────────────────────────────────────────────
def test_classifies_archived_or_moved() -> None:
    for code in (404, 410, 403):
        r = jira_probe.classify_probe_response("PROJ-3", code, {})
        assert r.branch == inbound_probe.ProbeBranch.ARCHIVED_OR_MOVED, code


def test_classifies_unreachable() -> None:
    for code in (500, 502, 503, 401):
        r = jira_probe.classify_probe_response("PROJ-4", code, {})
        assert r.branch == inbound_probe.ProbeBranch.UNREACHABLE, code


def test_request_is_get_only() -> None:
    req = jira_probe._make_request("https://example.atlassian.net", "PROJ-1", "user", "tok")
    assert req.get_method() == "GET"


def test_missing_env_raises_probe_config_error(monkeypatch) -> None:
    for var in ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REBAR_ROOT", "/nonexistent-so-no-config")
    with pytest.raises(inbound_probe.ProbeConfigError, match="JIRA_"):
        jira_probe._resolve_env()


# ── capability-LACKING backend degrades to UNREACHABLE (the key edge) ────────
def _make_probe_mutation(mut_mod, target: str = "PROJ-1"):
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.probe,
        target=target,
        payload={"reason": "absent_partner"},
        provenance={"source": "differ", "local_target": "LOCAL-1"},
    )


def test_dispatch_degrades_to_unreachable_without_capability() -> None:
    mut_mod = _load("reconcile_mutation", "mutation.py")
    run_differs = _load("run_differs_probe_heldout", "run_differs.py")

    class NonProbingBackend:
        """A backend WITHOUT SupportsAbsenceProbe (no probe_remote)."""

    captured: list = []

    def route(mut, result):
        captured.append(result)
        return None

    muts = [_make_probe_mutation(mut_mod, "PROJ-9")]
    run_differs._run_differs_inbound_probe_dispatch(muts, route, NonProbingBackend())

    assert captured, "route_inbound_probe should still be called (conservative branch)"
    assert captured[0].branch == inbound_probe.ProbeBranch.UNREACHABLE
    assert captured[0].issue_key == "PROJ-9"


# ── the neutral vocabulary stayed at root, with no vendor import ─────────────
def test_root_inbound_probe_is_neutral_vocabulary() -> None:
    """Root ``inbound_probe.py`` still exports the neutral vocab and imports nothing from
    ``adapters.jira``/``acli_subprocess`` at any depth."""
    assert {b.value for b in inbound_probe.ProbeBranch} == {
        "present_resolved",
        "present_filtered",
        "archived_or_moved",
        "unreachable",
    }
    assert issubclass(inbound_probe.ProbeConfigError, RuntimeError)

    tree = ast.parse((_REC / "inbound_probe.py").read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
            modules.update(f"{node.module or ''}.{a.name}" for a in node.names)
    offenders = sorted(m for m in modules if "adapters.jira" in m or "acli_subprocess" in m)
    assert not offenders, f"root inbound_probe.py must stay neutral; imports: {offenders}"
