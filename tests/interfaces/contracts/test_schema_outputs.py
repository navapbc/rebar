"""Every distinct rebar JSON output validates against its canonical schema.

Generalizes the ticket_state prototype (test_schema_ticket_state.py) to the full
set of output shapes (#2-#13 of the "--output everywhere" epic). For each shape
this drives REAL output from the live engine (library where exposed, CLI for the
rest) and validates it against the schema via the registry-aware validator (so
cross-file ``$ref``s to common.schema.json resolve).

It also pins two invariants:
  * every schema file is itself a valid draft-2020-12 schema, and
  * every schema named in ``schemas.OUTPUT_SCHEMAS`` exists on disk
(the coverage-guard test in T5 then closes the loop the other way — that no
structured output lacks a schema).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar import schemas

jsonschema = pytest.importorskip("jsonschema")
pytest.importorskip("referencing")


def _cli(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def _cli_json(*args: str, cwd: str):
    cp = _cli(*args, cwd=cwd)
    return json.loads(cp.stdout)


# ── schema-document + registry invariants ─────────────────────────────────────
def test_all_schemas_are_valid_documents() -> None:
    for name in schemas.names():
        jsonschema.Draft202012Validator.check_schema(schemas.load(name))


def test_registry_resolves_cross_file_refs() -> None:
    # Building a validator for a schema that $ref's common must not raise.
    schemas.validator(schemas.TICKET_STATE)
    schemas.validator(schemas.DEPS_GRAPH)


def test_output_schema_map_names_exist() -> None:
    on_disk = set(schemas.names())
    for key, name in schemas.OUTPUT_SCHEMAS.items():
        assert name in on_disk, f"OUTPUT_SCHEMAS[{key!r}] -> missing schema {name!r}"


# ── real-output conformance, one shape at a time ──────────────────────────────
def _seed(repo: Path) -> dict:
    r = str(repo)
    epic = rebar.create_ticket("epic", "Epic", repo_root=r)
    task = rebar.create_ticket(
        "task",
        "Task",
        description="Body\n\n## Acceptance Criteria\n- [ ] a",
        parent=epic,
        repo_root=r,
    )
    rebar.set_file_impact(task, [{"path": "a.py", "reason": "r"}], repo_root=r)
    rebar.set_verify_commands(
        task, [{"dd_id": "D1", "dd_text": "t", "command": "echo"}], repo_root=r
    )
    return {"epic": epic, "task": task, "repo": r}


def test_ticket_state_show_list_search(rebar_repo: Path) -> None:
    s = _seed(rebar_repo)
    v = schemas.validator(schemas.TICKET_STATE)
    v.validate(rebar.show_ticket(s["task"], repo_root=s["repo"]))
    for t in rebar.list_tickets(repo_root=s["repo"]):
        v.validate(t)
    for t in rebar.search("Task", repo_root=s["repo"]):
        v.validate(t)


def test_ticket_state_llm(rebar_repo: Path) -> None:
    s = _seed(rebar_repo)
    v = schemas.validator(schemas.TICKET_STATE_LLM)
    v.validate(_cli_json("show", s["task"], "--output", "llm", cwd=s["repo"]))
    for line in _cli("list", "--output", "llm", cwd=s["repo"]).stdout.splitlines():
        if line.strip():
            v.validate(json.loads(line))


def test_deps_graph(rebar_repo: Path) -> None:
    s = _seed(rebar_repo)
    schemas.validator(schemas.DEPS_GRAPH).validate(rebar.deps(s["task"], repo_root=s["repo"]))


def test_next_batch_and_limit_zero(rebar_repo: Path) -> None:
    s = _seed(rebar_repo)
    v = schemas.validator(schemas.NEXT_BATCH)
    v.validate(rebar.next_batch(s["epic"], repo_root=s["repo"]))
    v.validate(_cli_json("next-batch", s["epic"], "--limit=0", "--output", "json", cwd=s["repo"]))


def test_list_descendants(rebar_repo: Path) -> None:
    s = _seed(rebar_repo)
    schemas.validator(schemas.LIST_DESCENDANTS).validate(
        _cli_json("list-descendants", s["epic"], cwd=s["repo"])
    )


def test_clarity_result(rebar_repo: Path) -> None:
    s = _seed(rebar_repo)
    schemas.validator(schemas.CLARITY_RESULT).validate(
        rebar.clarity_check(s["task"], repo_root=s["repo"])
    )


def test_validate_report(rebar_repo: Path) -> None:
    s = _seed(rebar_repo)
    schemas.validator(schemas.VALIDATE_REPORT).validate(rebar.validate(repo_root=s["repo"]))


def test_file_impact_and_verify_commands(rebar_repo: Path) -> None:
    s = _seed(rebar_repo)
    schemas.validator(schemas.FILE_IMPACT).validate(
        rebar.get_file_impact(s["task"], repo_root=s["repo"])
    )
    schemas.validator(schemas.VERIFY_COMMANDS).validate(
        rebar.get_verify_commands(s["task"], repo_root=s["repo"])
    )


def test_scratch_envelope(rebar_repo: Path) -> None:
    s = _seed(rebar_repo)
    v = schemas.validator(schemas.SCRATCH_ENVELOPE)
    v.validate(_cli_json("scratch", "set", s["task"], "k", "v", cwd=s["repo"]))
    v.validate(_cli_json("scratch", "get", s["task"], "k", cwd=s["repo"]))
    v.validate(_cli_json("scratch", "clear", s["task"], "k", cwd=s["repo"]))
    v.validate(_cli_json("scratch", "get", s["task"], "k", cwd=s["repo"]))  # miss


def test_error_envelope(rebar_repo: Path) -> None:
    cp = _cli("show", "no-such-ticket-xyz", cwd=str(rebar_repo))
    schemas.validator(schemas.ERROR_ENVELOPE).validate(json.loads(cp.stdout))


# ── lifecycle result shapes (T3) ──────────────────────────────────────────────
def test_lifecycle_result_shapes(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    # create
    created = _cli_json("create", "task", "Lifecycle", "--output", "json", cwd=r)
    schemas.validator(schemas.CREATE_RESULT).validate(created)
    tid = created["id"]
    # claim
    schemas.validator(schemas.CLAIM_RESULT).validate(
        _cli_json("claim", tid, "--assignee=alice", "--output", "json", cwd=r)
    )
    # transition (and reopen, same shape)
    schemas.validator(schemas.TRANSITION_RESULT).validate(
        _cli_json("transition", tid, "in_progress", "closed", "--output", "json", cwd=r)
    )
    schemas.validator(schemas.TRANSITION_RESULT).validate(
        _cli_json("reopen", tid, "--output", "json", cwd=r)
    )
    # delete
    schemas.validator(schemas.DELETE_RESULT).validate(
        _cli_json("delete", tid, "--user-approved", "--output", "json", cwd=r)
    )


def test_lifecycle_results_via_library(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    created = rebar.create_ticket("task", "Lib lifecycle", return_alias=True, repo_root=r)
    schemas.validator(schemas.CREATE_RESULT).validate(created)
    tid = created["id"]
    schemas.validator(schemas.CLAIM_RESULT).validate(rebar.claim(tid, assignee="bob", repo_root=r))
    schemas.validator(schemas.TRANSITION_RESULT).validate(
        rebar.transition(tid, "in_progress", "closed", repo_root=r)
    )
    schemas.validator(schemas.TRANSITION_RESULT).validate(rebar.reopen(tid, repo_root=r))


# ── report command shapes (T4) ────────────────────────────────────────────────
def test_gate_results(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    tid = rebar.create_ticket(
        "task",
        "Gate probe",
        description="Body line.\n\n## Acceptance Criteria\n- [ ] a\n- [ ] b",
        repo_root=r,
    )
    v = schemas.validator(schemas.GATE_RESULT)
    # CLI --output json
    v.validate(_cli_json("check-ac", tid, "--output", "json", cwd=r))
    v.validate(_cli_json("quality-check", tid, "--output", "json", cwd=r))
    # library (adds `passed`)
    v.validate(rebar.check_ac(tid, repo_root=r))
    v.validate(rebar.quality_check(tid, repo_root=r))


def test_summary_shape(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    a = rebar.create_ticket("task", "S a", repo_root=r)
    b = rebar.create_ticket("task", "S b", repo_root=r)
    v = schemas.validator(schemas.SUMMARY)
    v.validate(_cli_json("summary", a, b, "--output", "json", cwd=r))
    v.validate(rebar.summary(a, b, repo_root=r))


def test_list_epics_wrapper_shape(rebar_repo: Path) -> None:
    # list-epics is now a DEPRECATED thin wrapper over the generic list: always
    # exit 0, {p0_bugs, epics} (ticket_state arrays), deprecation warning on stderr.
    import warnings

    r = str(rebar_repo)
    v = schemas.validator(schemas.LIST_EPICS)
    # no epics yet -> exit 0, empty epics, valid shape, warning on stderr (not stdout)
    cp = _cli("list-epics", "--output", "json", cwd=r)
    assert cp.returncode == 0
    d = json.loads(cp.stdout)
    v.validate(d)
    assert d["epics"] == []
    assert "deprecated" in cp.stderr.lower()
    # one open (unblocked) epic -> appears
    rebar.create_ticket("epic", "E1", repo_root=r)
    cp = _cli("list-epics", "--output", "json", cwd=r)
    assert cp.returncode == 0
    out = json.loads(cp.stdout)
    v.validate(out)
    assert len(out["epics"]) == 1
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        v.validate(rebar.list_epics(repo_root=r))


def test_fsck_and_bridge_fsck_shapes(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    schemas.validator(schemas.FSCK).validate(_cli_json("fsck", "--output", "json", cwd=r))
    schemas.validator(schemas.BRIDGE_FSCK).validate(
        _cli_json("bridge-fsck", "--output", "json", cwd=r)
    )
    schemas.validator(schemas.BRIDGE_FSCK).validate(rebar.bridge_fsck(repo_root=r))


# ── signing result shapes (sign / verify-signature) ───────────────────────────
def test_sign_and_verify_signature_shapes(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    signed = rebar.create_ticket("task", "To sign", repo_root=r)
    unsigned = rebar.create_ticket("task", "Unsigned", repo_root=r)

    # sign --output json -> the persisted SIGNATURE record + ticket_id
    sign_out = _cli_json("sign", signed, '["ran tests", "lint clean"]', "--output", "json", cwd=r)
    schemas.validator(schemas.SIGN_RESULT).validate(sign_out)
    assert sign_out["manifest"] == ["ran tests", "lint clean"]

    v = schemas.validator(schemas.VERIFY_SIGNATURE_RESULT)
    # a freshly-signed ticket verifies as `certified`
    certified = _cli_json("verify-signature", signed, "--output", "json", cwd=r)
    v.validate(certified)
    assert certified["verdict"] == "certified" and certified["verified"] is True
    # an unsigned ticket verifies as `unsigned` (null base fields)
    none = _cli_json("verify-signature", unsigned, "--output", "json", cwd=r)
    v.validate(none)
    assert none["verdict"] == "unsigned" and none["verified"] is False

    # library parity (sign_manifest / verify_signature)
    schemas.validator(schemas.SIGN_RESULT).validate(
        rebar.sign_manifest(unsigned, ["lib step"], repo_root=r)
    )
    v.validate(rebar.verify_signature(signed, repo_root=r))


def test_verify_signature_not_found_envelope(rebar_repo: Path) -> None:
    # The unresolved-ticket path emits an error_envelope, not the verdict shape.
    cp = _cli("verify-signature", "no-such-ticket-xyz", "--output", "json", cwd=str(rebar_repo))
    schemas.validator(schemas.ERROR_ENVELOPE).validate(json.loads(cp.stdout))


# ── MCP typed returns advertise an outputSchema ───────────────────────────────
def test_mcp_read_tools_advertise_output_schema(rebar_repo: Path) -> None:
    """Every typed read tool advertises an MCP outputSchema (so agents get a
    documented, validated shape). Lifecycle/gate tools whose shapes change in the
    T3/T4 stories are typed there, not here."""
    import asyncio

    from rebar.mcp_server import build_server

    typed_read_tools = {
        "show_ticket",
        "list_tickets",
        "search",
        "ticket_deps",
        "ready_tickets",
        "next_batch",
        "clarity_check",
        "validate",
        "get_file_impact",
        "get_verify_commands",
        "check_ac",
        "quality_check",
    }
    tools = {t.name: t for t in asyncio.run(build_server().list_tools())}
    for name in typed_read_tools:
        assert tools[name].outputSchema, f"{name} should advertise an outputSchema"
