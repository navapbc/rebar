"""CLI + MCP interface coverage for ``verify-completion`` / ``verify_completion`` (epic c7c5).

These tests exercise the two non-library interfaces OFFLINE by monkeypatching
``rebar.llm.verify_completion`` (the CLI and MCP tool both call it by module attribute, so
the patch is seen) — NO model, NO network, NO ``[agents]`` extra exercised. They assert the
observable interface contracts:

  CLI ``rebar verify-completion``:
    * ``--check`` is the offline preflight: exit 0 + a JSON backends report (no stack import);
    * a missing ``ticket_id`` is a usage error (argparse exits 2);
    * PASS ⇒ exit 0, FAIL ⇒ exit 1 (scriptable, like ``verify-signature``);
    * ``-o json`` stdout validates against the canonical ``completion_verdict`` schema;
    * ``-o text`` renders the verdict + each finding's criterion / detail / citation;
    * an ``LLMError`` surfaces as ``Error:`` + exit 1 with NO Python traceback.

  MCP ``verify_completion``:
    * registered; gated OFF by default ⇒ the call errors *because it is gated*;
    * gate ON + (monkeypatched) ⇒ returns a schema-valid ``completion_verdict`` DICT;
    * remains a NO_SCHEMA_EXEMPT (advertises no outputSchema — model-produced result).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar import schemas

# ── canned verdicts (the op's normalize/reconcile already ran upstream of the seam) ──
_PASS = {
    "verdict": "PASS",
    "findings": [],
    "summary": "all criteria met",
    "runner": "fake",
    "model": None,
    "trace_id": None,
    "reviewers": ["completion-verifier"],
}


def _pass(ticket_id, **kw):
    return {**_PASS, "target": {"kind": "ticket", "ticket_ids": [ticket_id]}}


def _fail(ticket_id, **kw):
    return {
        "verdict": "FAIL",
        "runner": "fake",
        "model": None,
        "trace_id": None,
        "reviewers": ["completion-verifier"],
        "target": {"kind": "ticket", "ticket_ids": [ticket_id]},
        "findings": [
            {
                "criterion": "AC1 — the thing exists",
                "detail": "no implementation found",
                "severity": "high",
                "dimension": "completion",
                "citations": [{"kind": "file", "path": "src/x.py", "line_start": 12}],
            }
        ],
    }


def _seed(repo: Path) -> str:
    return rebar.create_ticket(
        "task",
        "verify task",
        description="Body.\n\n## Acceptance Criteria\n- [ ] the thing exists\n",
        repo_root=str(repo),
    )


# ── CLI ────────────────────────────────────────────────────────────────────────
def test_cli_check_is_offline_and_exits_zero(rebar_repo: Path, capsys) -> None:
    """``--check`` never imports the stack, exits 0, and emits a JSON backends report."""
    from rebar._cli import main

    rc = main(["verify-completion", "--check"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)  # a valid JSON object
    assert isinstance(data, dict) and "pydantic_ai" in data


def test_cli_missing_ticket_id_is_a_usage_error(rebar_repo: Path) -> None:
    """A missing positional ``ticket_id`` is an argparse usage error (SystemExit 2),
    not a silent success or a crash."""
    from rebar._cli import main

    with pytest.raises(SystemExit) as ei:
        main(["verify-completion"])
    assert ei.value.code == 2


def test_cli_pass_exits_zero(rebar_repo: Path, monkeypatch, capsys) -> None:
    from rebar._cli import main

    monkeypatch.setattr(rebar.llm, "verify_completion", _pass)
    tid = _seed(rebar_repo)
    rc = main(["verify-completion", tid])
    assert rc == 0  # PASS ⇒ exit 0


def test_cli_fail_exits_one(rebar_repo: Path, monkeypatch, capsys) -> None:
    from rebar._cli import main

    monkeypatch.setattr(rebar.llm, "verify_completion", _fail)
    tid = _seed(rebar_repo)
    rc = main(["verify-completion", tid])
    assert rc == 1  # FAIL ⇒ exit 1 (scriptable)


def test_cli_json_output_validates_against_schema(rebar_repo: Path, monkeypatch, capsys) -> None:
    """CONTRACT: ``-o json`` emits a document that validates against the canonical
    ``completion_verdict`` schema (the pinned --output shape)."""
    from rebar._cli import main

    monkeypatch.setattr(rebar.llm, "verify_completion", _fail)
    tid = _seed(rebar_repo)
    rc = main(["verify-completion", tid, "-o", "json"])
    out = capsys.readouterr().out
    assert rc == 1
    payload = json.loads(out)
    schemas.validator(schemas.COMPLETION_VERDICT).validate(payload)


def test_cli_text_output_renders_verdict_and_findings(
    rebar_repo: Path, monkeypatch, capsys
) -> None:
    """``-o text`` renders the verdict and, for each finding, its criterion + detail +
    citation location — the operator-facing fields (asserted by their VALUES being present,
    which the renderer must surface, not by exact layout)."""
    from rebar._cli import main

    monkeypatch.setattr(rebar.llm, "verify_completion", _fail)
    tid = _seed(rebar_repo)
    rc = main(["verify-completion", tid, "-o", "text"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out  # the verdict
    assert "AC1 — the thing exists" in out  # the criterion
    assert "no implementation found" in out  # the detail
    assert "src/x.py" in out  # the citation location


def test_cli_llm_error_is_clean_exit_one(rebar_repo: Path, monkeypatch, capsys) -> None:
    """A typed ``LLMError`` (e.g. missing extra) surfaces as a clean ``Error:`` line on stderr
    with exit 1 — never a raw Python traceback (automation must not mistake it for success)."""
    from rebar._cli import main
    from rebar.llm.errors import LLMError

    def _boom(ticket_id, **kw):
        raise LLMError("the agent runner needs the 'agents' extra")

    monkeypatch.setattr(rebar.llm, "verify_completion", _boom)
    tid = _seed(rebar_repo)
    rc = main(["verify-completion", tid])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Error:" in err and "Traceback" not in err


# ── MCP ─────────────────────────────────────────────────────────────────────────
def _build_mcp():
    pytest.importorskip("mcp")
    from rebar.mcp_server import build_server

    return build_server()


def test_mcp_verify_completion_gated_off_by_default(rebar_repo: Path, monkeypatch) -> None:
    """Default MCP deployment: ``verify_completion`` is registered but DISABLED unless
    REBAR_MCP_ALLOW_LLM is set — the call errors *because it is gated* (not for an unrelated
    reason), so a default client can never trigger a billable LLM call. ``verify_completion``
    must NOT be reached (we patch it to assert it is never called)."""
    import asyncio

    from adapters import _unwrap  # tests/interfaces on sys.path

    def _never(*a, **k):
        raise AssertionError("verify_completion ran while the MCP gate was OFF")

    monkeypatch.delenv("REBAR_MCP_ALLOW_LLM", raising=False)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)
    srv = _build_mcp()
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    assert "verify_completion" in tools
    tid = _seed(rebar_repo)
    with pytest.raises(Exception) as exc:  # noqa: B017 — FastMCP wraps the ValueError
        _unwrap(asyncio.run(srv.call_tool("verify_completion", {"ticket_id": tid})))
    assert "disabled" in str(exc.value).lower(), str(exc.value)


def test_mcp_verify_completion_gate_on_returns_schema_valid_dict(
    rebar_repo: Path, monkeypatch
) -> None:
    """Gate ON + a monkeypatched verifier ⇒ the MCP tool returns a plain DICT that validates
    against the canonical ``completion_verdict`` schema (offline; no live call)."""
    import asyncio

    from adapters import _unwrap

    monkeypatch.setenv("REBAR_MCP_ALLOW_LLM", "1")
    monkeypatch.setattr(rebar.llm, "verify_completion", _fail)
    srv = _build_mcp()
    tid = _seed(rebar_repo)
    res = _unwrap(asyncio.run(srv.call_tool("verify_completion", {"ticket_id": tid})))
    assert isinstance(res, dict)
    schemas.validator(schemas.COMPLETION_VERDICT).validate(res)
    assert res["verdict"] == "FAIL"
    assert res["findings"][0]["criterion"].startswith("AC1")


def test_mcp_verify_completion_advertises_no_output_schema(rebar_repo: Path) -> None:
    """CONTRACT: ``verify_completion`` is a documented NO_SCHEMA_EXEMPT — it returns a
    model-produced dict and advertises NO outputSchema (so it is never auto-driven in CI)."""
    import asyncio

    srv = _build_mcp()
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    assert "verify_completion" in tools
    assert not tools["verify_completion"].outputSchema
