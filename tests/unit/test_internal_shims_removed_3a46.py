"""Held-out oracle for janitor story 3a46: internal shim removals."""

from __future__ import annotations

import rebar._cli as cli
import rebar.graph as graph
import rebar.reducer as reducer
from rebar._cli import _help_route
from rebar.graph import _loader
from rebar.grounding import detectors as grounding_detectors
from rebar.grounding import oracle
from rebar.grounding.detectors import registry as detector_registry
from rebar.llm.code_review import detectors
from rebar.reducer import _processors, _processors_status


def test_cli_help_private_forwarders_are_gone_but_canonical_route_still_works(
    capsys,
) -> None:
    assert not hasattr(cli, "_wants_help")
    assert not hasattr(cli, "_help_requested")
    assert not hasattr(cli, "_emit_subcommand_help")

    assert _help_route.wants_help(["task", "--help"]) is True
    assert _help_route.help_requested("create", ["task", "--help"]) is True
    assert _help_route.emit_subcommand_help("show") == 0
    assert "Usage: rebar show" in capsys.readouterr().out


def test_security_detector_shim_is_gone_and_failclosed_uses_canonical_runner(
    monkeypatch,
) -> None:
    assert not hasattr(detectors, "run_security_detectors")

    def fake_run_detectors(**kwargs):
        assert kwargs == {"changed_files": ["app.py"], "repo_root": None}
        return {
            "high-critical-security": {
                "matches": [{"location": {"file": "app.py", "line": 1}, "message": "secret"}],
                "abstained": [],
            }
        }

    monkeypatch.setattr(detectors, "run_detectors", fake_run_detectors)
    verdict = {"verdict": "PASS"}
    detectors.apply_failclosed(verdict, changed_files=["app.py"], repo_root=None)

    assert verdict["verdict"] == "BLOCK"
    assert verdict["coverage"]["security_detectors"][0]["criterion"] == "high-critical-security"
    assert verdict["blocking"][0]["location"] == "app.py"


def test_graph_private_reducer_bindings_are_gone_but_loader_patch_still_intercepts(
    monkeypatch,
    tmp_path,
) -> None:
    assert not hasattr(graph, "_reduce_ticket")
    assert not hasattr(graph, "_reducer")

    observed: list[tuple[str, object]] = []

    def fake_reduce_all_tickets(path, **kwargs):
        assert kwargs == {"exclude_archived": False, "exclude_session_logs": True}
        observed.append(("reduce_all_tickets", path))
        return [{"ticket_id": "a", "deps": [], "status": "open"}]

    monkeypatch.setattr(_loader.reducer, "reduce_all_tickets", fake_reduce_all_tickets)

    assert graph.build_dep_graph("a", str(tmp_path))["ready_to_work"] is True
    assert observed == [("reduce_all_tickets", str(tmp_path))]


def test_reducer_root_no_longer_exports_split_processor_shims() -> None:
    for name in (
        "process_archived",
        "process_bridge_alert",
        "process_comment",
        "process_create",
        "process_edit",
        "process_link",
        "process_revert",
        "process_snapshot",
        "process_status",
        "process_unlink",
    ):
        assert not hasattr(reducer, name)

    assert callable(_processors.process_create)
    assert callable(_processors.process_comment)
    assert callable(_processors_status.process_status)


def test_grounding_dimensions_reexport_is_gone_but_oracle_remains_authoritative() -> None:
    assert not hasattr(detector_registry, "DIMENSIONS")
    assert not hasattr(grounding_detectors, "DIMENSIONS")

    assert oracle.DIMENSIONS == detector_registry._canonical_dimensions()
    assert oracle.is_known_dimension("touches_auth") is True
