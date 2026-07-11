"""Every MCP tool that advertises an outputSchema returns data that validates
against its canonical JSON Schema — or is a documented exemption.

Sub-effort (d) of story fatty-cipher-range / ticket wine-yield-scene.

``test_schema_outputs.py`` pinned a hand-picked subset; this closes the loop:

  * the set of tools under test is sourced MECHANICALLY from ``list_tools()``
    (not a hand list) — a newly-added outputSchema tool fails this test until it
    is classified, so coverage can never silently regress;
  * every canonical-backed tool is driven on a real fixture store and its result
    is ``jsonschema``-validated against the canonical schema named in
    docs/output-schemas.md;
  * the remaining advertisers are explicitly recorded as EXEMPT with a reason
    (generic ``{result: ...}`` ack/string wrappers that have no canonical shape).

It also pins the reverse gap: tools that HAVE a canonical schema but deliberately
advertise no outputSchema (``transition``/``reopen`` — the ``from`` reserved word)
stay a documented, closed set.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import rebar
from rebar import schemas

jsonschema = pytest.importorskip("jsonschema")
pytest.importorskip("referencing")

from adapters import _unwrap  # noqa: E402  (tests/interfaces is on sys.path)

# ── disposition: every outputSchema advertiser is classified here ─────────────
# value is either a canonical schema name (validate the real result against it)
# or EXEMPT with a one-line reason.
EXEMPT = "EXEMPT"

# Canonical-backed tools: MCP tool name -> canonical schema name.
CANONICAL: dict[str, str] = {
    "show_ticket": schemas.TICKET_STATE,
    "list_tickets": schemas.TICKET_STATE,
    "search": schemas.TICKET_STATE,
    "ready_tickets": schemas.TICKET_STATE,
    "recent_session_logs": schemas.TICKET_STATE,
    "ticket_deps": schemas.DEPS_GRAPH,
    "next_batch": schemas.NEXT_BATCH,
    "clarity_check": schemas.CLARITY_RESULT,
    "check_ac": schemas.GATE_RESULT,
    "quality_check": schemas.GATE_RESULT,
    "validate": schemas.VALIDATE_REPORT,
    "get_file_impact": schemas.FILE_IMPACT,
    "get_verify_commands": schemas.VERIFY_COMMANDS,
    "create_ticket": schemas.CREATE_RESULT,
    "create_idea": schemas.CREATE_RESULT,
    "log_session": schemas.CREATE_RESULT,
    "claim_ticket": schemas.CLAIM_RESULT,
    "summary": schemas.SUMMARY,
    "bridge_fsck": schemas.BRIDGE_FSCK,
    "sign_manifest": schemas.SIGN_RESULT,
    "verify_signature": schemas.VERIFY_SIGNATURE_RESULT,
    "get_workflow_status": schemas.WORKFLOW_RUN,
    "get_workflow_result": schemas.WORKFLOW_RUN,
    "grounding_info": schemas.GROUNDING_INFO,
}

# Advertisers with no canonical structured shape — they return a generic
# {result: <str>} ack wrapper auto-derived from a `-> str` return annotation.
EXEMPT_GENERIC: dict[str, str] = {
    "comment_ticket": "string ack ('Commented on …'); no canonical shape",
    "tag_ticket": "string ack; no canonical shape",
    "untag_ticket": "string ack; no canonical shape",
    "archive_ticket": "string ack; no canonical shape",
    "compact_ticket": "string ack; no canonical shape",
    "edit_ticket": "string ack; no canonical shape",
    "link_tickets": "string ack; no canonical shape",
    "unlink_tickets": "string ack; no canonical shape",
    "set_file_impact": "string ack; no canonical shape",
    "set_verify_commands": "string ack; no canonical shape",
    "fsck": "MCP fsck returns a human summary string; the canonical `fsck` schema "
    "describes the CLI/library `--output json` shape, not the MCP string",
    "render_workflow": "workflow engine (WS-I): returns a Mermaid flowchart as a "
    "string (a read-only render); no canonical structured shape.",
}

# Tools that HAVE a canonical schema but advertise NO outputSchema by design.
NO_SCHEMA_EXEMPT: dict[str, str] = {
    "explain_criterion": "plan-review criteria authoring-guide lookup (epic cite-stone-sea / "
    "WS10): a pure registry/guide READ that returns a plain dict — {criterion_id, section} on "
    "success or {error, kind} on failure — a FREE-FORM doc section, not a schema-backed model, "
    "so it advertises NO outputSchema by design.",
    "transition_ticket": "returns {ticket_id, from, to, …}; `from` is a Python "
    "reserved word, so it returns a plain dict (no pydantic "
    "model). CLI/library JSON pinned to transition_result.",
    "reopen_ticket": "same {…, from, …} shape as transition; reserved word.",
    "reconcile": "no canonical schema for the reconcile plan/result shape.",
    "review_ticket": "rebar.llm review op: makes a live LLM call, so it returns a "
    "plain dict (review_result shape) and advertises NO outputSchema "
    "by design — it must NOT be auto-driven on the fixture store in "
    "CI. The CLI/library --output json path IS pinned to "
    "review_result via OUTPUT_SCHEMAS['review'].",
    "review_code": "rebar.llm code-review op: live LLM call(s) over a git range, "
    "returns an aggregated review_result as a plain dict (no "
    "outputSchema) — same exemption rationale as review_ticket.",
    "scan_spec": "rebar.llm batch spec-scan op: live LLM call(s) over the store's "
    "epics, returns a review_result as a plain dict (no outputSchema) "
    "— same exemption rationale as review_ticket.",
    "verify_completion": "rebar.llm completion-verification op: live LLM call, returns a "
    "completion_verdict as a plain dict (no outputSchema) — same exemption rationale as "
    "review_ticket. CLI/library --output json is pinned via OUTPUT_SCHEMAS['verify_completion'].",
    "review_plan": "rebar.llm plan-review gate (epic 5fd2): live LLM call(s), returns a "
    "plan_review_verdict as a plain dict (no outputSchema) — same model-produced exemption "
    "rationale as verify_completion. Inverse of the completion-verification close gate.",
    "sign_review": "rebar.llm cheap re-sign path (ticket middle-actinium-thrush): re-persists a "
    "plan-review attestation from the latest REVIEW_RESULT sidecar with NO LLM call, returning a "
    "plain {ok, signed, ticket_id, verdict, reason, signature?} recovery dict (no outputSchema).",
    "run_workflow": "workflow engine (WS-C4): async — returns {run_id, ticket_id, "
    "status:'running'} immediately and runs in the background; a plain dict (no "
    "outputSchema) because it is a fire-and-forget START ack, not the run result "
    "(the typed surface is get_workflow_status/result, validated below).",
}


def _tools() -> dict:
    from rebar.mcp_server import build_server

    return {t.name: t for t in asyncio.run(build_server().list_tools())}


def _advertisers() -> set[str]:
    return {n for n, t in _tools().items() if t.outputSchema}


# ── completeness guards (mechanical, sourced from list_tools()) ───────────────
def test_every_advertiser_is_classified() -> None:
    classified = set(CANONICAL) | set(EXEMPT_GENERIC)
    advertised = _advertisers()
    unclassified = advertised - classified
    assert not unclassified, (
        f"MCP tools advertise an outputSchema but are not classified in this test: "
        f"{sorted(unclassified)} — validate them against a canonical schema or add a "
        f"documented EXEMPT_GENERIC entry."
    )
    # And nothing stale: every classified name must really still advertise one.
    stale = classified - advertised
    assert not stale, f"classified tools no longer advertise an outputSchema: {sorted(stale)}"


def test_no_schema_advertisers_are_exhaustively_classified() -> None:
    """Every MCP tool returning STRUCTURED data is in EXACTLY ONE classification
    set — closing the gap where a structured-dict tool that advertised no
    outputSchema (sign_manifest/verify_signature) sat in none of the three sets
    and so escaped every other guard.

    A tool is "structured" if it advertises an outputSchema (a dict/model return)
    OR is recorded in NO_SCHEMA_EXEMPT (a structured dict deliberately advertising
    none). The only tools legitimately outside all three sets are the generic
    string-ack writers, which surface as ``-> str`` advertisers and are caught
    here as CANONICAL/EXEMPT_GENERIC members. This guard asserts the partition is
    total and disjoint, so an unclassified structured tool can never recur.
    """
    sets = {
        "CANONICAL": set(CANONICAL),
        "EXEMPT_GENERIC": set(EXEMPT_GENERIC),
        "NO_SCHEMA_EXEMPT": set(NO_SCHEMA_EXEMPT),
    }
    # Disjointness: no tool may be classified in two sets at once.
    names = list(sets)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            overlap = sets[names[i]] & sets[names[j]]
            assert not overlap, f"{names[i]} and {names[j]} both classify: {sorted(overlap)}"

    classified = set().union(*sets.values())

    # TOTAL partition: EVERY tool sourced mechanically from list_tools() is
    # classified. This is the load-bearing assertion — a brand-new tool (a typed
    # advertiser, or a plain-dict/string-ack writer with no outputSchema) that the
    # author forgets to classify lands outside all three sets and trips here. That
    # is exactly the class of gap (an untyped structured tool in no set) this guard
    # exists to make impossible.
    all_tools = set(_tools())
    assert all_tools <= classified, (
        f"MCP tools in no classification set (add each to CANONICAL, EXEMPT_GENERIC, "
        f"or NO_SCHEMA_EXEMPT): {sorted(all_tools - classified)}"
    )

    # Every advertiser must be classified (CANONICAL or EXEMPT_GENERIC).
    advertised = _advertisers()
    assert advertised <= classified, (
        f"advertised MCP tools not in any classification set: {sorted(advertised - classified)}"
    )

    # And every structured tool that advertises NO schema must be in
    # NO_SCHEMA_EXEMPT — the exact class of gap this test exists to catch.
    structured_no_schema = set(NO_SCHEMA_EXEMPT) - advertised
    unclassified = structured_no_schema - classified
    assert not unclassified, (
        f"structured-dict MCP tools advertise no outputSchema and are in no "
        f"classification set: {sorted(unclassified)}"
    )


def test_no_schema_exempt_set_is_accurate() -> None:
    # The reverse gap: tools we deliberately leave without an outputSchema must
    # genuinely lack one (so this exemption list cannot rot).
    tools = _tools()
    for name in NO_SCHEMA_EXEMPT:
        assert name in tools, f"{name} no longer exists as a tool"
        assert not tools[name].outputSchema, (
            f"{name} now advertises an outputSchema — remove it from NO_SCHEMA_EXEMPT "
            f"and validate it against its canonical schema instead."
        )


# ── real-result conformance for canonical-backed advertisers ──────────────────
def _seed(repo: Path) -> dict:
    r = str(repo)
    epic = rebar.create_ticket("epic", "Epic", repo_root=r)
    task = rebar.create_ticket(
        "task",
        "Task",
        description="Body.\n\n## Acceptance Criteria\n- [ ] a",
        parent=epic,
        repo_root=r,
    )
    rebar.set_file_impact(task, [{"path": "a.py", "reason": "r"}], repo_root=r)
    rebar.set_verify_commands(
        task, [{"dd_id": "D1", "dd_text": "t", "command": "echo"}], repo_root=r
    )
    # A spare open ticket to exercise claim_ticket without disturbing `task`.
    claimable = rebar.create_ticket("task", "Claimable", repo_root=r)
    # A session_log so recent_session_logs returns a non-empty list to shape-check
    # (hidden from list/search/ready, so it does not disturb the other tools).
    log = rebar.create_ticket("session_log", "Session log", description="verbose", repo_root=r)
    # A persisted workflow run so get_workflow_status/result have something to read
    # (dry_run = offline FakeRunner, no tokens). Single agent step -> terminal.
    run = rebar.run_workflow(
        {
            "schema_version": "1",
            "name": "guard_demo",
            "steps": [{"id": "review", "prompt": "code_quality", "mode": "findings"}],
        },
        ticket_id=task,
        dry_run=True,
        repo_root=r,
    )
    return {
        "epic": epic,
        "task": task,
        "claimable": claimable,
        "log": log,
        "repo": r,
        "run_id": run["run_id"],
    }


def _call_args(name: str, s: dict) -> dict:
    return {
        "show_ticket": {"ticket_id": s["task"]},
        "list_tickets": {},
        "search": {"query": "Task"},
        "ready_tickets": {},
        "recent_session_logs": {},
        "ticket_deps": {"ticket_id": s["task"]},
        "next_batch": {"epic_id": s["epic"]},
        "clarity_check": {"ticket_id": s["task"]},
        "check_ac": {"ticket_id": s["task"]},
        "quality_check": {"ticket_id": s["task"]},
        "validate": {},
        "get_file_impact": {"ticket_id": s["task"]},
        "get_verify_commands": {"ticket_id": s["task"]},
        "create_ticket": {"ticket_type": "task", "title": "Made by MCP"},
        "create_idea": {"title": "Made by MCP"},
        "log_session": {"entry": "a verbose log entry"},
        "claim_ticket": {"ticket_id": s["claimable"], "assignee": "agent"},
        "summary": {"ticket_ids": [s["task"]]},
        "bridge_fsck": {},
        "sign_manifest": {"ticket_id": s["task"], "manifest": ["step one", "step two"]},
        "verify_signature": {"ticket_id": s["task"]},
        "get_workflow_status": {"run_id": s["run_id"], "ticket_id": s["task"]},
        "get_workflow_result": {"run_id": s["run_id"], "ticket_id": s["task"]},
        "grounding_info": {},
    }[name]


@pytest.mark.parametrize("name", sorted(CANONICAL))
def test_canonical_tool_result_validates(name: str, rebar_repo: Path) -> None:
    from rebar.mcp_server import build_server

    s = _seed(rebar_repo)
    srv = build_server()
    result = _unwrap(asyncio.run(srv.call_tool(name, _call_args(name, s))))

    schema = schemas.load(CANONICAL[name])
    validator = schemas.validator(CANONICAL[name])

    # An array-typed canonical schema validates the whole list; an object-typed
    # one validates each element when the tool returns a list (show/list/ready
    # share the single-ticket ticket_state schema).
    if schema.get("type") == "array":
        validator.validate(result)
    elif isinstance(result, list):
        assert result, f"{name} returned an empty list; cannot confirm item shape"
        for item in result:
            validator.validate(item)
    else:
        validator.validate(result)
