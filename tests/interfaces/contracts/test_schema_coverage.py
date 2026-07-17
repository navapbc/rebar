"""Coverage guard: structured-output coverage is ENFORCED, not aspirational.

Four invariants close the loop opened by test_schema_outputs.py (which drives
each known shape and validates it):

  1. Every schema file shipped under src/rebar/schemas/ (except the shared
     common.schema.json) is wired into the OUTPUT_SCHEMAS registry — no
     authored-but-unreferenced schema.
  2. Every OUTPUT_SCHEMAS entry resolves to a real schema file.
  3. Every CLI command whose `--help` advertises the canonical `--output` flag is
     represented in OUTPUT_SCHEMAS — so adding `--output` to a NEW command
     without authoring + registering its schema fails this test.
  4. Every CLI `--output` advertiser is driven on a real fixture store and its
     REAL output is jsonschema-validated against the registered schema — the
     CLI-side mirror of the MCP completeness guard
     (test_mcp_output_schema_coverage.py). Membership alone (3) does not prove the
     command actually emits the registered shape; (4) does, and a newly-added
     advertiser falls outside the classification map and trips the guard until it
     is driven or explicitly exempted.

(3)/(4) discover commands straight from the dispatcher's per-subcommand help
source, so they can't drift from what the CLI actually offers.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from rebar import schemas


def test_every_schema_file_is_wired() -> None:
    wired = set(schemas.OUTPUT_SCHEMAS.values())
    # COMMON holds shared $defs; INPUT_SCHEMAS validate input documents (the
    # workflow DSL files) and CONTRACT_SCHEMAS are per-step I/O contracts consumed by
    # the inspector + linter — none advertise a command's output, so none is wired
    # into OUTPUT_SCHEMAS by design.
    exempt = {schemas.COMMON} | set(schemas.INPUT_SCHEMAS) | set(schemas.CONTRACT_SCHEMAS)
    for name in schemas.names():
        if name in exempt:
            continue
        assert name in wired, (
            f"schema {name!r} exists on disk but is not referenced by "
            f"schemas.OUTPUT_SCHEMAS (wire it or delete it)"
        )


def test_every_registry_entry_resolves() -> None:
    on_disk = set(schemas.names())
    for key, name in schemas.OUTPUT_SCHEMAS.items():
        assert name in on_disk, f"OUTPUT_SCHEMAS[{key!r}] -> missing schema {name!r}"


def _help_arms() -> dict[str, str]:
    """Map each subcommand -> its `--help` text from the in-process help system
    (``rebar._cli._help``), the authoritative per-command usage."""
    from rebar._cli import _help

    return {sub: (_help.subcommand_help(sub) or "") for sub in _help.known_subcommands()}


def test_commands_advertising_output_have_a_schema() -> None:
    arms = _help_arms()
    assert arms, "could not parse any subcommand help arms (parser drift?)"
    missing = []
    for cmd, help_text in arms.items():
        if "--output" not in help_text:
            continue
        key = cmd.replace("-", "_")  # CLI uses hyphens; registry keys use underscores
        if key not in schemas.OUTPUT_SCHEMAS:
            missing.append(cmd)
    assert not missing, (
        "these commands advertise --output in their help but lack a schema in "
        f"schemas.OUTPUT_SCHEMAS: {sorted(missing)}"
    )


# ── (4) mechanical real-output validation for every CLI --output advertiser ────
# The CLI mirror of test_mcp_output_schema_coverage.py: each advertiser is driven
# on a real fixture store and its REAL `--output json` is validated against the
# registered schema. The set of advertisers is sourced MECHANICALLY from the help
# arms (test_commands_advertising_output_have_a_schema), so a NEW advertiser the
# author forgets to wire here lands outside this map and trips the completeness
# guard below — coverage can't silently regress.
#
# Value is either a builder that, given the seeded fixture ids, returns the CLI
# args after the subcommand (the command emits its registered schema as JSON), or
# EXEMPT_BRIDGE for the one advertiser whose JSON shape requires a bridge run.
EXEMPT_BRIDGE = "EXEMPT_BRIDGE"

# advertiser command (hyphenated) -> args-after-subcommand builder | EXEMPT_BRIDGE.
CLI_OUTPUT_DRIVERS: dict[str, object] = {
    "show": lambda s: ["show", s["task"]],
    "list": lambda s: ["list"],
    "ready": lambda s: ["ready"],
    "session-logs": lambda s: ["session-logs"],
    "next-batch": lambda s: ["next-batch", s["epic"], "--limit=0"],
    "summary": lambda s: ["summary", s["task"]],
    "check-ac": lambda s: ["check-ac", s["task"]],
    "quality-check": lambda s: ["quality-check", s["task"]],
    "validate": lambda s: ["validate"],
    "fsck": lambda s: ["fsck"],
    "bridge-fsck": lambda s: ["bridge-fsck"],
    "grounding-info": lambda s: ["grounding-info"],
    "create": lambda s: ["create", "task", "Made by guard"],
    "idea": lambda s: ["idea", "Made by guard"],
    "claim": lambda s: ["claim", s["claimable"], "--assignee=agent"],
    "transition": lambda s: ["transition", s["openish"], "open", "in_progress"],
    "reopen": lambda s: ["reopen", s["closed"]],
    "sign": lambda s: ["sign", s["task"], '["did the thing"]'],
    "verify-signature": lambda s: ["verify-signature", s["task"]],
    "delete": lambda s: ["delete", s["doomed"], "--user-approved"],
    # bridge-status only emits its registered JSON shape AFTER a reconcile bridge
    # run has written a status file; with no bridge it prints a human hint + exit 1.
    # Driving it would require standing up the Jira bridge, so it is documented
    # EXEMPT here (mirrors the env-dependent exemptions in the MCP guard).
    "audit": lambda s: ["audit", "show", s["task"]],
    "bridge-status": EXEMPT_BRIDGE,
}


def _output_advertisers() -> set[str]:
    return {cmd for cmd, help_text in _help_arms().items() if "--output" in help_text}


def test_every_cli_output_advertiser_is_classified() -> None:
    """Mechanical completeness: every CLI --output advertiser is either driven by
    CLI_OUTPUT_DRIVERS (validated below) or explicitly EXEMPT — so a new advertiser
    cannot be added without classifying it."""
    advertised = _output_advertisers()
    classified = set(CLI_OUTPUT_DRIVERS)
    unclassified = advertised - classified
    assert not unclassified, (
        f"CLI commands advertise --output but are not classified for real-output "
        f"validation: {sorted(unclassified)} — add a driver to CLI_OUTPUT_DRIVERS "
        f"(or mark EXEMPT_BRIDGE with a reason)."
    )
    stale = classified - advertised
    assert not stale, (
        f"CLI_OUTPUT_DRIVERS lists commands that no longer advertise --output: {sorted(stale)}"
    )


def _cli_json(repo: Path, *args: str):
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args, "--output", "json"],
        capture_output=True,
        text=True,
        cwd=str(repo),
    )
    assert cp.stdout.strip(), f"cli {args} produced no stdout (rc={cp.returncode}, {cp.stderr})"
    return cp.stdout


def _seed_for_cli(repo: Path) -> dict:
    import rebar

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
    # Spares so claim/transition/reopen/delete don't disturb `task`.
    claimable = rebar.create_ticket("task", "Claimable", repo_root=r)
    openish = rebar.create_ticket("task", "Transitionable", repo_root=r)
    closed = rebar.create_ticket("task", "To reopen", repo_root=r)
    rebar.transition(closed, "open", "in_progress", repo_root=r)
    rebar.transition(closed, "in_progress", "closed", repo_root=r)
    doomed = rebar.create_ticket("task", "Doomed", repo_root=r)
    return {
        "epic": epic,
        "task": task,
        "claimable": claimable,
        "openish": openish,
        "closed": closed,
        "doomed": doomed,
        "repo": r,
    }


_DRIVEN = sorted(c for c, v in CLI_OUTPUT_DRIVERS.items() if v != EXEMPT_BRIDGE)


@pytest.mark.parametrize("cmd", _DRIVEN)
def test_cli_output_advertiser_real_output_validates(cmd: str, rebar_repo: Path) -> None:
    """Drive each CLI --output advertiser on a real store and validate its REAL
    JSON against the registered schema — proving the advertised flag emits the
    shape it claims, not just that a registry entry exists."""
    pytest.importorskip("jsonschema")
    pytest.importorskip("referencing")

    s = _seed_for_cli(rebar_repo)
    args = CLI_OUTPUT_DRIVERS[cmd](s)  # type: ignore[operator]
    out = _cli_json(rebar_repo, *args)

    schema_name = schemas.OUTPUT_SCHEMAS[cmd.replace("-", "_")]
    schema = schemas.load(schema_name)
    validator = schemas.validator(schema_name)

    # Parse the whole document if it is one JSON value (possibly pretty-printed
    # across lines); fall back to JSON-lines (one record per line) otherwise.
    try:
        payload = json.loads(out)
        records = [payload]
    except json.JSONDecodeError:
        records = [json.loads(ln) for ln in out.splitlines() if ln.strip()]

    for payload in records:
        if schema.get("type") == "array":
            # An array-typed schema validates the whole list at once.
            validator.validate(payload)
        elif isinstance(payload, list):
            # Object schema + the command returns a list (list/ready/search share
            # the single-ticket ticket_state schema) -> validate each element.
            for item in payload:
                validator.validate(item)
        else:
            validator.validate(payload)


def test_output_schemas_pin_creation_channel_trust_boundary() -> None:
    """The public output-schema docs MUST pin the creation-channel diagnostic trust
    boundary (epic jira-reb-977): ``creation_channel_inferred`` is heuristic AUDIT
    metadata, NOT a security attestation. Pinned here so the distinction cannot silently
    drift out of docs/output-schemas.md."""
    text = (Path(__file__).resolve().parents[3] / "docs" / "output-schemas.md").read_text()
    lowered = text.lower()
    assert "creation_channel_inferred" in text
    assert "audit" in lowered, "trust-boundary must call the marker AUDIT metadata"
    assert "security attestation" in lowered, "trust-boundary must deny a security attestation"
