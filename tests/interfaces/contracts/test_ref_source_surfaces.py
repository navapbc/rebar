"""S5 — surface the ref + source controls on the CLI and MCP tool schemas (epic raze-vet-ditch).

Both human (CLI) and agent (MCP) clients can target the exact code version a gate runs
against. Covers the pinned one-to-one CLI<->MCP mapping over the five code-reading ops, the
defaults, the threading, the failure paths (invalid --source / unresolvable ref fail closed),
and surfacing verified_at_sha.
"""

from __future__ import annotations

import asyncio

import pytest

import rebar
import rebar.llm  # noqa: F401
from rebar._cli import _llm_commands as cli

# The pinned one-to-one CLI<->MCP mapping (the five code-reading operations).
_MAPPING = {
    "review_ticket": cli._review,
    "review_code": cli._review_code,
    "scan_spec": cli._scan_spec,
    "verify_completion": cli._verify_completion,
    "review_plan": cli._review_plan,
}


# --------------------------------------------------------------------------------------
# MCP side: every code-reading tool advertises ref + source
# --------------------------------------------------------------------------------------
def test_mcp_tools_expose_ref_and_source():
    from rebar.mcp_server import build_server

    tools = {t.name: t for t in asyncio.run(build_server().list_tools())}
    for name in _MAPPING:
        assert name in tools, f"MCP tool {name} missing"
        props = tools[name].inputSchema.get("properties", {})
        assert "ref" in props, f"{name} lacks ref"
        assert "source" in props, f"{name} lacks source"


# --------------------------------------------------------------------------------------
# CLI side: every counterpart command accepts --ref / --source and threads them through
# --------------------------------------------------------------------------------------
def _patch_all(monkeypatch, capture: dict):
    """Stub every llm gate op to capture (ref, source) + return a canned attested result."""

    def _mk(name):
        def _fn(*a, **kw):
            capture[name] = (kw.get("ref"), kw.get("source"))
            return {
                "verdict": "PASS",
                "findings": [],
                "target": {"kind": "ticket", "ticket_ids": ["t"]},
                "runner": "fake",
                "model": "m",
                "source": kw.get("source") or "attested",
                "verified_at_sha": None if kw.get("source") == "local" else "deadbeef",
                "signable": kw.get("source") != "local",
            }

        return _fn

    for op in (
        "review_ticket",
        "review_code",
        "scan_epics_for_spec",
        "verify_completion",
        "review_plan",
    ):
        monkeypatch.setattr(rebar.llm, op, _mk(op))


def test_cli_threads_ref_source(rebar_repo, monkeypatch, tmp_path):
    cap: dict = {}
    _patch_all(monkeypatch, cap)
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    monkeypatch.chdir(rebar_repo)
    spec = tmp_path / "spec.txt"
    spec.write_text("spec")

    cli._review([tid, "--ref", "release/x", "--source", "local"])
    assert cap["review_ticket"] == ("release/x", "local")

    cli._review_code(["--diff-file", str(spec), "--ref", "feat/y", "--source", "attested"])
    assert cap["review_code"] == ("feat/y", "attested")

    cli._scan_spec(["--spec-file", str(spec), "--ref", "main", "--source", "local"])
    assert cap["scan_epics_for_spec"] == ("main", "local")

    cli._verify_completion([tid, "--ref", "v1.2", "--source", "attested"])
    assert cap["verify_completion"] == ("v1.2", "attested")

    cli._review_plan([tid, "--ref", "develop", "--source", "local"])
    assert cap["review_plan"] == ("develop", "local")


def test_cli_defaults_are_none_so_config_resolves(rebar_repo, monkeypatch):
    """Omitting the flags passes None → the configured default (origin/main, attested)
    resolves downstream — the defaults are NOT hardcoded into the CLI."""
    cap: dict = {}
    _patch_all(monkeypatch, cap)
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    monkeypatch.chdir(rebar_repo)
    cli._verify_completion([tid])
    assert cap["verify_completion"] == (None, None)


# --------------------------------------------------------------------------------------
# failure paths
# --------------------------------------------------------------------------------------
def test_invalid_source_is_rejected(rebar_repo, monkeypatch):
    monkeypatch.chdir(rebar_repo)
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    with pytest.raises(SystemExit) as exc:  # argparse choices -> exit 2
        cli._review([tid, "--source", "bogus"])
    assert exc.value.code == 2


def test_unresolvable_ref_fails_closed_with_clean_error(rebar_repo, monkeypatch, capsys):
    from rebar._snapshot import SnapshotRefError

    def _boom(*a, **kw):
        raise SnapshotRefError("cannot resolve ref 'nope'")

    monkeypatch.setattr(rebar.llm, "verify_completion", _boom)
    monkeypatch.chdir(rebar_repo)
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rc = cli._verify_completion([tid, "--ref", "nope", "--source", "attested"])
    assert rc == 1  # fails closed, not a traceback
    assert "Error:" in capsys.readouterr().err


# --------------------------------------------------------------------------------------
# verified_at_sha is surfaced in text output (attested) / unsigned note (local)
# --------------------------------------------------------------------------------------
def test_text_output_surfaces_verified_at_sha(rebar_repo, monkeypatch, capsys):
    cap: dict = {}
    _patch_all(monkeypatch, cap)
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    monkeypatch.chdir(rebar_repo)
    cli._review([tid, "--source", "attested", "-o", "text"])
    out = capsys.readouterr().out
    assert "source: attested" in out and "verified-at-sha deadbeef" in out

    cli._review([tid, "--source", "local", "-o", "text"])
    out2 = capsys.readouterr().out
    assert "source: local" in out2 and "unsigned" in out2
