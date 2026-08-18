"""Held-out oracle for 8a31 — the teeth. NOT shown to the implementer.

Validates the cross-interface, per-code, regression, soft-error, schema, and message-safety
contracts that separate a real single-vocabulary implementation from one that just makes the
happy path pass.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src" / "rebar"


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------
def _clean_env(root: Path) -> dict:
    env = subprocess_env(REBAR_ROOT=str(root))
    for var in ("REBAR_TRACKER_DIR", "REBAR_TRACKER_BRANCH", "REBAR_CONFIG"):
        env.pop(var, None)
    return env


def _pin(root: Path) -> None:
    """Pin the in-process env so library calls read the tracker the subprocess helpers wrote."""
    os.environ["REBAR_ROOT"] = str(root)
    for var in ("REBAR_TRACKER_DIR", "REBAR_TRACKER_BRANCH", "REBAR_CONFIG"):
        os.environ.pop(var, None)


def _structured_payload(res: object) -> dict:
    """Dig the structured dict out of a call_tool result across SDK return shapes."""
    if isinstance(res, tuple):  # (content, structured) on older SDKs
        for part in res:
            if isinstance(part, dict) and "error" in part:
                return part
        res = res[0]
    if isinstance(res, dict):
        return res
    if isinstance(res, (list, tuple)):  # list[TextContent] — JSON in .text
        for block in res:
            text = getattr(block, "text", None)
            if text:
                return json.loads(text)
    raise AssertionError(f"could not extract structured payload from {res!r}")


def _fresh_tracker(tmp: Path) -> Path:
    env = _clean_env(tmp)
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True, env=env)
    subprocess.run(["rebar", "init"], cwd=tmp, check=True, capture_output=True, env=env)
    return tmp


def _cli_json(root: Path, *args: str) -> tuple[dict, int]:
    p = subprocess.run(
        ["rebar", *args], cwd=root, env=_clean_env(root), capture_output=True, text=True
    )
    return json.loads(p.stdout.strip().splitlines()[-1]), p.returncode


def _mcp_call(tool: str, args: dict, root: Path):
    os.environ["REBAR_ROOT"] = str(root)
    for var in ("REBAR_TRACKER_DIR", "REBAR_TRACKER_BRANCH", "REBAR_CONFIG"):
        os.environ.pop(var, None)
    from rebar.mcp_server import build_server

    return asyncio.run(build_server().call_tool(tool, args))


def _mcp_envelope_for(tool: str, args: dict, root: Path) -> dict:
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as ei:
        _mcp_call(tool, args, root)
    return ei.value.__cause__.envelope


def _new_ticket(root: Path) -> str:
    p = subprocess.run(
        ["rebar", "create", "task", "t"],
        cwd=root,
        env=_clean_env(root),
        capture_output=True,
        text=True,
    )
    return re.search(r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}", p.stdout).group(0)


# ---------------------------------------------------------------------------
# E1 — the three interfaces agree on the SAME code for the SAME failure (AC5).
# ---------------------------------------------------------------------------
def test_cross_interface_agreement_missing_ticket(tmp_path: Path) -> None:
    import rebar

    root = _fresh_tracker(tmp_path)
    missing = "abcd-1234-5678-9abc"

    # library
    try:
        rebar.show_ticket(missing)
        raise AssertionError("expected failure")
    except Exception as exc:  # noqa: BLE001
        lib_code = rebar.error_code_for(exc)

    cli_env, _ = _cli_json(root, "show", missing, "--output", "json")
    mcp_env = _mcp_envelope_for("show_ticket", {"ticket_id": missing}, root)

    assert lib_code == cli_env["error"] == mcp_env["error"] == "ticket_not_found"


def test_cross_interface_agreement_concurrency(tmp_path: Path) -> None:
    import rebar
    from rebar._errors import ConcurrencyError

    root = _fresh_tracker(tmp_path)
    tid = _new_ticket(root)
    _pin(root)
    rebar.claim(tid)

    # library: a second claim rejects with ConcurrencyError (no .message attr)
    try:
        rebar.claim(tid)
        raise AssertionError("expected concurrency rejection")
    except ConcurrencyError as exc:
        lib_code = rebar.error_code_for(exc)

    tid2 = _new_ticket(root)
    rebar.claim(tid2)
    cli_env, cli_rc = _cli_json(root, "claim", tid2, "--output", "json")

    tid3 = _new_ticket(root)
    rebar.claim(tid3)
    mcp_env = _mcp_envelope_for("claim_ticket", {"ticket_id": tid3}, root)

    assert lib_code == cli_env["error"] == mcp_env["error"] == "concurrency_conflict"
    assert cli_rc == 10


# ---------------------------------------------------------------------------
# E2 — per-code pinning: the machine code is stable regardless of human message (AC4).
# ---------------------------------------------------------------------------
def test_error_code_is_message_independent() -> None:
    import rebar
    from rebar._engine_support.reads import TicketNotFoundError
    from rebar._errors import ConcurrencyError, RebarError

    assert (
        rebar.error_code_for(TicketNotFoundError("Ticket 'a' not found"))
        == rebar.error_code_for(TicketNotFoundError("totally different wording"))
        == "ticket_not_found"
    )
    assert (
        rebar.error_code_for(ConcurrencyError("already claimed"))
        == rebar.error_code_for(ConcurrencyError("someone else holds it, ¯\\_(ツ)_/¯"))
        == "concurrency_conflict"
    )
    assert (
        rebar.error_code_for(RebarError("x"))
        == rebar.error_code_for(RebarError("y"))
        == "command_failed"
    )


# ---------------------------------------------------------------------------
# E3 — vocabulary closure: every CLI literal is a member; the classifier only emits members (AC2).
# ---------------------------------------------------------------------------
def test_every_cli_literal_is_a_known_code() -> None:
    import rebar

    literals: set[str] = set()
    pat = re.compile(r"""error_envelope\(\s*["']([a-z_]+)["']""")
    for path in SRC.rglob("*.py"):
        literals.update(pat.findall(path.read_text(encoding="utf-8")))

    assert literals, "expected to find error_envelope literal call sites"
    unknown = literals - rebar.KNOWN_ERROR_CODES
    assert not unknown, f"CLI emits codes absent from KNOWN_ERROR_CODES: {sorted(unknown)}"


def test_classifier_only_returns_known_codes() -> None:
    import rebar
    from rebar._commands.txn import ConcurrencyMismatch
    from rebar._engine_support.reads import TicketNotFoundError
    from rebar._errors import ConcurrencyError, RebarError, TrackerRootError
    from rebar.llm.errors import LLMError

    for exc in (
        TicketNotFoundError("not found"),
        ConcurrencyError("x"),
        ConcurrencyMismatch("x"),
        TrackerRootError("x"),
        LLMError("x"),
        RebarError("x"),
    ):
        assert rebar.error_code_for(exc) in rebar.KNOWN_ERROR_CODES


def test_concurrency_mismatch_maps_like_concurrency_error() -> None:
    import rebar
    from rebar._commands.txn import ConcurrencyMismatch

    # ConcurrencyMismatch is a CommandError subclass carrying NO error_code — must still map.
    assert rebar.error_code_for(ConcurrencyMismatch("not open")) == "concurrency_conflict"


# ---------------------------------------------------------------------------
# E4 — CLI envelopes are byte-identical to today (AC6). deps MUST stay deps_failed (F5).
# ---------------------------------------------------------------------------
def test_cli_show_envelope_unchanged(tmp_path: Path) -> None:
    root = _fresh_tracker(tmp_path)
    env, _rc = _cli_json(root, "show", "abcd-1234-5678-9abc", "--output", "json")
    assert env == {
        "error": "ticket_not_found",
        "input": "abcd-1234-5678-9abc",
        "message": "Ticket 'abcd-1234-5678-9abc' not found",
        "exit_code": 1,
    }


def test_cli_deps_still_emits_deps_failed(tmp_path: Path) -> None:
    # reads.py:469 raises a bare ReadError ("does not exist"); the deps reader keys the
    # message and emits deps_failed. Retargeting it to ticket_not_found would break AC6.
    root = _fresh_tracker(tmp_path)
    env, _rc = _cli_json(root, "deps", "abcd-1234-5678-9abc")
    assert env["error"] == "deps_failed"
    assert env["message"] == "ticket 'abcd-1234-5678-9abc' does not exist"


# ---------------------------------------------------------------------------
# E5 — the two RETURN-shaped soft errors gain a vocabulary code but KEEP their agent fields.
# ---------------------------------------------------------------------------
def test_explain_criterion_soft_error_gains_code_keeps_kind(tmp_path: Path) -> None:
    root = _fresh_tracker(tmp_path)
    os.environ["REBAR_ROOT"] = str(root)
    from rebar.mcp_server import build_server

    res = asyncio.run(build_server().call_tool("explain_criterion", {"criterion_id": "no-such-id"}))
    # call_tool returns a list of content blocks; the soft-error dict is JSON in the first
    # TextContent.text (older SDKs return a (content, structured) tuple — handle both).
    payload = _structured_payload(res)

    import rebar

    assert payload["error"] in rebar.KNOWN_ERROR_CODES
    assert payload["error"] == "criterion_unknown_id"
    assert payload["kind"]  # the agent-branching field is preserved


def test_structured_llm_failure_gains_code_keeps_disposition() -> None:
    import rebar
    from rebar._mcp_llm import _structured_llm_failure
    from rebar.llm.errors import LLMError

    out = _structured_llm_failure(LLMError("provider overloaded"))
    assert out["error"] in rebar.KNOWN_ERROR_CODES
    # the disposition fields agents branch on are preserved
    assert "resolution_class" in out
    assert "retryable" in out
    assert "diagnostic" in out


# ---------------------------------------------------------------------------
# E6 — schema: a target-less envelope (no input) validates against the relaxed schema.
# ---------------------------------------------------------------------------
def test_schema_allows_input_optional_but_still_accepts_cli_shape() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema_doc = json.loads((SRC / "schemas" / "common.schema.json").read_text())
    defs = schema_doc.get("$defs") or schema_doc["definitions"]
    env_schema = defs["error_envelope"]

    # a gate refusal not tied to a ticket: no input
    jsonschema.validate({"error": "command_failed", "message": "refused"}, env_schema)
    # the CLI shape (with input + exit_code) still validates
    jsonschema.validate(
        {"error": "ticket_not_found", "input": "x", "message": "m", "exit_code": 1}, env_schema
    )


# ---------------------------------------------------------------------------
# E7 — message safety: the guard/classifier never touch a missing .message attribute.
# ---------------------------------------------------------------------------
def test_concurrency_envelope_uses_str_not_message_attr(tmp_path: Path) -> None:
    import rebar
    from rebar._errors import ConcurrencyError

    exc = ConcurrencyError("boom")
    assert not hasattr(exc, "message")  # the exact attribute the naive guard would read
    # classifier does not explode on the attribute-less exception
    assert rebar.error_code_for(exc) == "concurrency_conflict"

    root = _fresh_tracker(tmp_path)
    tid = _new_ticket(root)
    _pin(root)
    rebar.claim(tid)
    env = _mcp_envelope_for("claim_ticket", {"ticket_id": tid}, root)
    assert env["error"] == "concurrency_conflict"
    assert env["message"]  # str(exc)-derived, non-empty


# ---------------------------------------------------------------------------
# E8 — the guard must cover ASYNC tools (run_workflow is a coroutine): an async body
# raising a rebar exception still yields McpEnvelopeError; a non-rebar error propagates
# unchanged; and the wrapped callable stays a coroutine so FastMCP awaits it.
# ---------------------------------------------------------------------------
class _FakeMcp:
    """Minimal stand-in whose .tool records the (possibly wrapped) registered fn."""

    def __init__(self) -> None:
        self.registered: dict = {}

    def tool(self, *_a, **_k):
        def deco(fn):
            self.registered["fn"] = fn
            return fn

        return deco


def _guarded(fn):
    import inspect

    from rebar._mcp_errors import install_error_guard

    m = _FakeMcp()
    install_error_guard(m)
    m.tool()(fn)
    wrapped = m.registered["fn"]
    assert inspect.iscoroutinefunction(wrapped) == inspect.iscoroutinefunction(fn), (
        "guard must preserve async-ness so FastMCP awaits the coroutine"
    )
    return wrapped


def test_guard_wraps_async_tool_rebar_error_to_envelope() -> None:
    from rebar._errors import ConcurrencyError
    from rebar._mcp_errors import McpEnvelopeError

    async def boom(**_):
        raise ConcurrencyError("already claimed")

    with pytest.raises(McpEnvelopeError) as ei:
        asyncio.run(_guarded(boom)())
    assert ei.value.envelope["error"] == "concurrency_conflict"
    assert ei.value.envelope["message"]


def test_guard_lets_async_non_rebar_error_propagate() -> None:
    async def fence(**_):
        raise ValueError("run_workflow requires allow_llm")

    with pytest.raises(ValueError, match="allow_llm"):
        asyncio.run(_guarded(fence)())


def test_guard_wraps_sync_tool_rebar_error_to_envelope() -> None:
    from rebar._errors import TrackerRootError
    from rebar._mcp_errors import McpEnvelopeError

    def boom(**_):
        raise TrackerRootError("no tracker root")

    with pytest.raises(McpEnvelopeError) as ei:
        _guarded(boom)()
    assert ei.value.envelope["error"] == "tracker_root_unresolved"
