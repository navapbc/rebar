"""Oracle for the MCP-tool-surface vs library-facade parity gate.

Ticket 8ce5-b870-601d-4715 (cream-capitate-snake).

The gate compares two live surfaces — the MCP tool registry and the ``rebar`` library
facade — against a COMMITTED manifest, and fails when either side drifts from it.

Design contract these tests pin (mirroring ``scripts/check_verify_gate_parity.py``):
``evaluate(live, manifest)`` is PURE and injectable, so drift is provable on synthetic
data without mutating the repository; ``build_live_surface()`` does the real
introspection; ``main()`` wires them to ``--check`` / ``--update``.

The suite is deliberately weighted toward the FAILING directions. A drift gate that
cannot be shown to fail is indistinguishable from one that returns 0, so every drift
shape (added / removed / changed / undeclared / stale / malformed) has a case proving
it fails AND names the offending tool. Three of these were verified by defect-seeded
mutation: neutering the mandatory-reason validation reddens 3 cases, neutering the
symbol/correspondence coherence check reddens 1, and dropping the ``repo_root``
normalization rule reddens 1 — so the positive assertions are load-bearing too, not
tautologies that would pass against a permissive stub.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_mcp_library_parity.py"
MAKEFILE = REPO_ROOT / "Makefile"


def _load():
    spec = importlib.util.spec_from_file_location("check_mcp_library_parity", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


parity = _load()


def _tool(
    *,
    registrar: str = "write",
    library_symbol: str | None = "claim",
    correspondence: str = "co_names",
    mcp_params: list[str] | None = None,
    library_params: list[str] | None = None,
    divergence: dict | None = None,
) -> dict:
    """One normalized manifest/live descriptor."""
    entry = {
        "registrar": registrar,
        "library_symbol": library_symbol,
        "correspondence": correspondence,
        "mcp_params": ["ticket_id"] if mcp_params is None else mcp_params,
        "library_params": ["ticket_id"] if library_params is None else library_params,
    }
    if divergence is not None:
        entry["divergence"] = divergence
    return entry


def _manifest(tools: dict) -> dict:
    return {
        "schema_version": 1,
        "normalization": {"library_only_params": ["repo_root"]},
        "tools": tools,
    }


def test_evaluate_reports_parity_when_live_matches_manifest():
    """The base case: identical surfaces produce exit 0 and no drift diagnostics."""
    tools = {"claim_ticket": _tool()}
    code, messages = parity.evaluate(_manifest(tools), _manifest(tools))
    assert code == 0
    drift = [m for m in messages if "ADDED" in m or "REMOVED" in m or "CHANGED" in m]
    assert drift == [], f"expected no drift diagnostics, got {drift}"


def test_evaluate_accepts_a_tool_whose_surfaces_agree():
    """A tool whose MCP and library parameters agree needs no divergence declaration."""
    tools = {
        "show_ticket": _tool(
            registrar="read",
            library_symbol="show_ticket",
            correspondence="exact",
            mcp_params=["ticket_id"],
            library_params=["ticket_id"],
        )
    }
    code, _ = parity.evaluate(_manifest(tools), _manifest(tools))
    assert code == 0


def test_build_live_surface_enumerates_the_real_tool_registry():
    """The live surface is the real MCP registry: every tool carries a registrar and
    a normalized parameter list, and the three registrars are all represented."""
    live = parity.build_live_surface()
    tools = live["tools"]
    assert len(tools) >= 60, f"expected the full MCP tool surface, got {len(tools)}"
    assert {t["registrar"] for t in tools.values()} == {"read", "llm", "write"}
    for name, entry in tools.items():
        assert isinstance(entry["mcp_params"], list), name
        assert entry["correspondence"] in {"exact", "co_names", "mcp_only"}, name


def test_render_manifest_is_canonical_json():
    """The committed artifact is deterministic: sorted keys, 2-space indent, trailing
    newline — so a regeneration produces a reviewable diff, not a reordering."""
    tools = {"b_tool": _tool(), "a_tool": _tool()}
    text = parity.render_manifest(_manifest(tools))
    assert text.endswith("\n")
    parsed = json.loads(text)
    assert list(parsed["tools"]) == sorted(parsed["tools"])
    assert text == parity.render_manifest(json.loads(text))


def test_check_passes_against_the_committed_manifest(capsys):
    """The shipped manifest matches the shipped surfaces — the gate is green at rest,
    and says so: a silent exit 0 is indistinguishable from a gate that checked nothing,
    so the run must print a positive confirmation naming how many tools it covered
    (the `Verified-gate parity: OK (...)` house style)."""
    assert parity.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "OK" in out, f"expected a positive confirmation line, got: {out!r}"
    assert any(ch.isdigit() for ch in out), f"confirmation should name a count, got: {out!r}"


# --------------------------------------------------------------------------
# manifest-vs-live drift: every shape must fail and NAME the offending tool
# --------------------------------------------------------------------------


def test_a_tool_added_to_mcp_but_absent_from_the_manifest_fails():
    manifest = _manifest({"claim_ticket": _tool()})
    live = _manifest({"claim_ticket": _tool(), "brand_new_tool": _tool()})
    code, messages = parity.evaluate(live, manifest)
    assert code != 0
    assert any("brand_new_tool" in m for m in messages), messages


def test_a_tool_removed_from_mcp_but_still_in_the_manifest_fails():
    manifest = _manifest({"claim_ticket": _tool(), "retired_tool": _tool()})
    live = _manifest({"claim_ticket": _tool()})
    code, messages = parity.evaluate(live, manifest)
    assert code != 0
    assert any("retired_tool" in m for m in messages), messages


def test_a_changed_mcp_parameter_list_fails():
    """The live tool grew a parameter the manifest does not record."""
    manifest = _manifest({"claim_ticket": _tool(mcp_params=["ticket_id"])})
    live = _manifest({"claim_ticket": _tool(mcp_params=["ticket_id", "assignee"])})
    code, messages = parity.evaluate(live, manifest)
    assert code != 0
    assert any("claim_ticket" in m for m in messages), messages


def test_a_changed_library_symbol_mapping_fails():
    """The tool now wraps a different facade function than the manifest recorded."""
    manifest = _manifest({"claim_ticket": _tool(library_symbol="claim")})
    live = _manifest({"claim_ticket": _tool(library_symbol="transition")})
    code, messages = parity.evaluate(live, manifest)
    assert code != 0
    assert any("claim_ticket" in m for m in messages), messages


# --------------------------------------------------------------------------
# mcp-vs-library divergence: undeclared fails, declared-with-reason passes
# --------------------------------------------------------------------------


def test_undeclared_divergence_between_the_two_surfaces_fails():
    """MCP and library parameters disagree and nothing declares why."""
    tools = {
        "edit_ticket": _tool(
            library_symbol="edit_ticket",
            correspondence="exact",
            mcp_params=["ticket_id", "title", "priority"],
            library_params=["ticket_id", "fields"],
        )
    }
    code, messages = parity.evaluate(_manifest(tools), _manifest(tools))
    assert code != 0, "an undeclared divergence must fail even when live==manifest"
    assert any("edit_ticket" in m for m in messages), messages


def test_declared_divergence_with_a_reason_passes():
    tools = {
        "edit_ticket": _tool(
            library_symbol="edit_ticket",
            correspondence="exact",
            mcp_params=["ticket_id", "title", "priority"],
            library_params=["ticket_id", "fields"],
            divergence={
                "kind": "enumerated_vs_varkw",
                "reason": (
                    "library edit_ticket is a **fields passthrough; the MCP tool "
                    "enumerates parameters to keep the edit surface at CLI/library parity"
                ),
            },
        )
    }
    code, _ = parity.evaluate(_manifest(tools), _manifest(tools))
    assert code == 0


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_declared_divergence_with_a_blank_reason_fails(reason):
    """The reason is mandatory — the same convention as `# read-via:` in
    check_config_reads.py, where a bare marker is itself an error."""
    divergence = {"kind": "enumerated_vs_varkw"}
    if reason is not None:
        divergence["reason"] = reason
    tools = {
        "edit_ticket": _tool(
            library_symbol="edit_ticket",
            mcp_params=["ticket_id", "title"],
            library_params=["ticket_id", "fields"],
            divergence=divergence,
        )
    }
    code, messages = parity.evaluate(_manifest(tools), _manifest(tools))
    assert code != 0
    assert any("edit_ticket" in m for m in messages), messages


def test_a_declared_divergence_still_fails_once_the_real_shape_moves():
    """AC3's sharp edge: declaring a divergence pins the SHAPE it was declared for.
    If the live shape drifts away from the declared one, the waiver must not absorb it."""
    declared = _tool(
        library_symbol="edit_ticket",
        mcp_params=["ticket_id", "title"],
        library_params=["ticket_id", "fields"],
        divergence={"kind": "enumerated_vs_varkw", "reason": "documented passthrough"},
    )
    moved = _tool(
        library_symbol="edit_ticket",
        mcp_params=["ticket_id", "title", "surprise_new_param"],
        library_params=["ticket_id", "fields"],
        divergence={"kind": "enumerated_vs_varkw", "reason": "documented passthrough"},
    )
    code, messages = parity.evaluate(
        _manifest({"edit_ticket": moved}), _manifest({"edit_ticket": declared})
    )
    assert code != 0
    assert any("edit_ticket" in m for m in messages), messages


def test_mcp_only_tools_must_be_declared_as_such():
    """A tool with no library counterpart is legitimate (16 exist today) but must say so
    rather than silently reading as a broken mapping."""
    tools = {
        "audit_trail": _tool(
            registrar="read",
            library_symbol=None,
            correspondence="exact",  # wrong: it has no facade symbol
            mcp_params=["ticket_id"],
            library_params=None,
        )
    }
    code, messages = parity.evaluate(_manifest(tools), _manifest(tools))
    assert code != 0
    assert any("audit_trail" in m for m in messages), messages


# --------------------------------------------------------------------------
# normalization: repo_root is a category rule, not 35 per-tool waivers
# --------------------------------------------------------------------------


def test_repo_root_is_normalized_away_and_needs_no_per_tool_waiver():
    """Every exact-match tool omits the library's `repo_root` because MCP resolves the
    root from the server environment. Normalizing it once keeps 35 tools quiet; failing
    to normalize it would make the gate cry drift on the whole surface."""
    tools = {
        "show_ticket": _tool(
            registrar="read",
            library_symbol="show_ticket",
            correspondence="exact",
            mcp_params=["ticket_id"],
            library_params=["ticket_id", "repo_root"],
        )
    }
    code, messages = parity.evaluate(_manifest(tools), _manifest(tools))
    assert code == 0, f"repo_root should normalize away, got {messages}"


# --------------------------------------------------------------------------
# the real committed surface
# --------------------------------------------------------------------------


def test_the_committed_manifest_declares_the_known_edit_ticket_divergence():
    """The one divergence we know is real must be declared, with a non-empty reason."""
    manifest = parity.parse_manifest(Path(REPO_ROOT / parity.MANIFEST_PATH).read_text())
    entry = manifest["tools"]["edit_ticket"]
    assert "divergence" in entry, "edit_ticket's enumerated-vs-**fields split must be declared"
    assert entry["divergence"].get("reason", "").strip()


def test_the_committed_manifest_covers_every_live_tool():
    live = parity.build_live_surface()
    manifest = parity.parse_manifest(Path(REPO_ROOT / parity.MANIFEST_PATH).read_text())
    assert set(manifest["tools"]) == set(live["tools"])


def test_update_is_idempotent():
    """Regenerating twice yields identical bytes, so `--update` produces a clean diff."""
    live = parity.build_live_surface()
    once = parity.render_manifest(live)
    twice = parity.render_manifest(parity.parse_manifest(once))
    assert once == twice


# --------------------------------------------------------------------------
# CLI + wiring (E2E)
# --------------------------------------------------------------------------


def test_cli_check_exits_zero_on_the_committed_tree():
    """Run the gate exactly as `make lint` does.

    Spawned with cwd=REPO_ROOT deliberately, in a clean subprocess, to escape the unit
    tier's autouse monkeypatches — the same rationale as tests/unit/test_api_surface_gate.py.
    """
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,  # the exit code IS the assertion below
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_make_lint_runs_the_parity_check():
    """The gate is worthless unless the build actually runs it."""
    text = MAKEFILE.read_text()
    lint = text.split("lint:", 1)[1].split("\n\n", 1)[0]
    assert "check_mcp_library_parity.py" in lint, "parity gate is not wired into `make lint`"


def test_failure_output_is_actionable():
    """A drift message must name the regeneration command, not just say 'drift'."""
    manifest = _manifest({"claim_ticket": _tool()})
    live = _manifest({"claim_ticket": _tool(), "brand_new_tool": _tool()})
    _, messages = parity.evaluate(live, manifest)
    blob = "\n".join(messages)
    assert "--update" in blob, f"failure output should name the fix, got: {blob}"
