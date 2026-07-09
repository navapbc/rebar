"""rebar MCP server (FastMCP).

Exposes the ticket system as MCP tools, built on the rebar Python library.
Reads (``show``/``list``) run in-process via rebar._reads (no subprocess);
``reconcile`` defaults to a non-mutating dry-run.

Safety:
  * ``reconcile`` defaults to ``dry-run``; ``live`` additionally requires
    REBAR_MCP_ALLOW_JIRA_SYNC=1.
  * Write tools (create/transition/edit/link/unlink/tag/untag/archive/comment)
    are gated by REBAR_MCP_READONLY: set it to 1 to expose a read-only server.

The ``mcp`` dependency is an optional extra and is imported lazily.

Structure: ``build_server`` is a thin assembler — it builds the FastMCP server,
packs the shared handles + gate helpers into a ``ctx`` namespace, and calls the
three per-cluster registrars (``register_read_tools`` / ``register_llm_tools`` /
``register_write_tools`` in ``_mcp_reads`` / ``_mcp_llm`` / ``_mcp_writes``). The
gate helpers, the workflow-payload budget cap, ``_dump``, the ``MODE_CAPS`` table,
and the output models (re-exported from ``_mcp_models`` for back-compat) live here.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from types import SimpleNamespace

import rebar

# Output models live in the leaf module rebar._mcp_models (imported only by pydantic)
# so the per-cluster registrars can share them WITHOUT importing this module (which
# would form an import cycle). Re-exported here for back-compat: existing callers
# import e.g. ``rebar.mcp_server.NextBatchOut`` / ``ValidateReportOut`` directly.
from rebar._mcp_llm import register_llm_tools
from rebar._mcp_models import (
    BridgeFsckOut,
    ClaimResultOut,
    ClarityResultOut,
    CreateResultOut,
    DepsGraphOut,
    FileImpactItemOut,
    GateResultOut,
    GroundingBackendOut,
    GroundingInfoOut,
    NextBatchOut,
    SignResultOut,
    TicketStateOut,
    ValidateReportOut,
    VerifyCommandItemOut,
    VerifySignatureResultOut,
    WorkflowRunOut,
)
from rebar._mcp_reads import register_read_tools
from rebar._mcp_writes import register_write_tools

logger = logging.getLogger(__name__)

__all__ = [
    "BridgeFsckOut",
    "ClaimResultOut",
    "ClarityResultOut",
    "CreateResultOut",
    "DepsGraphOut",
    "FileImpactItemOut",
    "GateResultOut",
    "GroundingBackendOut",
    "GroundingInfoOut",
    "NextBatchOut",
    "SignResultOut",
    "TicketStateOut",
    "ValidateReportOut",
    "VerifyCommandItemOut",
    "VerifySignatureResultOut",
    "WorkflowRunOut",
    "MCP_ENV_VARS",
    "build_server",
    "main",
]


# ── Canonical MCP environment-variable contract ──────────────────────────────
# The SINGLE SOURCE OF TRUTH for the env vars the MCP server honors. The published
# manifest (server.json) MUST advertise exactly this set — a CI drift-guard
# (scripts/check_server_manifest.py, wired into .github/workflows/test.yml) diffs
# server.json against this list and fails the build on divergence, so the manifest
# can never silently drift from the real gates again. The ``--help`` text below is
# also derived from this list, so the three stay in lockstep.
#
# Each entry: name, a one-line description, and whether it is a deprecated alias.
# The active gates are read in mcp_server.build_server / _mcp_reads / _mcp_llm /
# _mcp_writes / config.py. (The REBAR_MCP_ALLOW_RECONCILE_LIVE alias of
# REBAR_MCP_ALLOW_JIRA_SYNC was removed pre-1.0 — DE7.)
MCP_ENV_VARS: tuple[dict, ...] = (
    {
        "name": "REBAR_ROOT",
        "description": (
            "Path to the repo root that holds the .tickets-tracker store "
            "(defaults to the git toplevel of the working dir)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_READONLY",
        "description": "Set to 1 to expose only the read tools (no write/mutation tools).",
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_ALLOW_LLM",
        "description": (
            "Set to 1 to enable the billable LLM tools (review_ticket / review_code / "
            "scan_spec / verify_completion / review_plan); off by default."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_ALLOW_JIRA_SYNC",
        "description": (
            "Set to 1 to allow the live (mutating) Jira reconcile mode; otherwise "
            "reconcile is dry-run only."
        ),
        "deprecated": False,
    },
)


# The reconcile tool gates modes by the engine's canonical MODE_CAPS table, which
# lives in the bundled engine at rebar_reconciler/mode.py. We load it ONCE here by
# FILE PATH (not `from rebar_reconciler.mode import ...`) and bind the names as
# module globals. Loading by path is deliberate: the dotted import is unreliable
# because the top-level name `rebar_reconciler` is shadowed in sys.modules in some
# contexts (notably the unit-test package of the same name under pytest), which
# makes `rebar_reconciler.mode` raise ModuleNotFoundError. mode.py is stdlib-only
# and self-contained, so a standalone path-load is safe.
def _load_engine_mode():
    from rebar._engine import engine_dir

    mode_path = engine_dir() / "rebar_reconciler" / "mode.py"
    spec = importlib.util.spec_from_file_location("rebar._engine_mode", mode_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MODE_CAPS, mod.Mode


MODE_CAPS, Mode = _load_engine_mode()


def _mcp_gate(attr: str, *, fail: bool) -> bool:
    """Resolve a typed ``mcp.<attr>`` boolean gate through the single-source config
    (env ``REBAR_MCP_<ATTR>`` wins over a ``[tool.rebar.mcp]`` config file; the
    ``_as_bool`` coercion accepts 1/true/yes/on, any case, whitespace-tolerant). On a
    MALFORMED config it returns ``fail`` — the SAFE direction for that gate, so the
    value reported by ``rebar config`` is exactly what's enforced here."""
    try:
        return getattr(rebar.config.load_config().mcp, attr)
    except rebar.config.ConfigError:
        return fail


def _readonly() -> bool:
    # Fail-CLOSED (read-only) on a malformed config — consistent with the verify
    # gate; a broken config hides the write tools rather than exposing them. Routed
    # through the ONE shared resolver in rebar.config so the LLM runner's read-only
    # gate (runner._readonly_gate) resolves identically and the two can't drift.
    # (_mcp_gate stays for the allow_llm / allow_jira_sync gates below.)
    return rebar.config.mcp_readonly()


def _allow_llm() -> bool:
    # Fail-SAFE off — a malformed config never enables billable LLM calls.
    return _mcp_gate("allow_llm", fail=False)


def _allow_jira_sync() -> bool:
    # Fail-SAFE off — a malformed config never enables live/applying Jira writes.
    return _mcp_gate("allow_jira_sync", fail=False)


# Keep MCP workflow status/result payloads under the client's ~25K-token budget
# (WS-ffc4). ~90 KB ≈ 25K tokens; over it, elide the bulky step outputs (which an
# agent can re-read via the library/CLI) while preserving the schema-valid shape.
_WORKFLOW_TOKEN_BUDGET_BYTES = 90_000


def _payload_bytes(payload: dict) -> int:
    import json

    return len(json.dumps(payload, default=str))


def _cap_workflow_payload(payload: dict) -> dict:
    """Bound a status/result payload under the ~25K-token MCP budget (WS-ffc4).

    Truncates the bulky carriers in escalating order until the WHOLE payload fits —
    bulk can live in `outputs`/`terminal_output` (result read) OR `steps` (status
    read) OR `error`/elsewhere — so the budget is airtight regardless of shape. The
    full result stays available via the library/CLI."""
    if _payload_bytes(payload) <= _WORKFLOW_TOKEN_BUDGET_BYTES:
        return payload
    note = (
        "[truncated to stay under the MCP token budget — read the full result via "
        "rebar.get_workflow_result / `rebar workflow result`]"
    )
    capped = dict(payload)
    capped["truncated"] = True
    # 1) elide the result carriers.
    if capped.get("terminal_output"):
        capped["terminal_output"] = {"_truncated": note}
    if isinstance(capped.get("outputs"), dict):
        capped["outputs"] = {sid: {"_truncated": note} for sid in capped["outputs"]}
    # 2) still over? collapse the per-step status map to a count (status read).
    if _payload_bytes(capped) > _WORKFLOW_TOKEN_BUDGET_BYTES and isinstance(
        capped.get("steps"), dict
    ):
        capped["steps"] = {"_truncated": f"{len(capped['steps'])} steps; {note}"}
    # 3) last resort: a minimal envelope that is guaranteed to fit + schema-valid.
    if _payload_bytes(capped) > _WORKFLOW_TOKEN_BUDGET_BYTES:
        capped = {
            "run_id": str(payload.get("run_id", "")),
            "status": str(payload.get("status", "")),
            "ticket_id": payload.get("ticket_id"),
            "workflow_name": payload.get("workflow_name"),
            "truncated": True,
            "error": note,
        }
    return capped


def _dump(item):
    """Normalize a typed list-item param to a plain dict (FastMCP may deliver a
    validated pydantic model or a raw dict depending on version). Drops keys whose
    value is None so the engine receives a clean {path,reason}/{dd_id,…} object."""
    if hasattr(item, "model_dump"):
        return {k: v for k, v in item.model_dump().items() if v is not None}
    return item


def build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit(
            "The rebar MCP server requires the 'mcp' extra. "
            "Install it with: pip install 'nava-rebar[mcp]'"
        ) from exc

    mcp = FastMCP("rebar")

    # Shared handles + gate helpers the tool closures capture. Each registrar rebinds
    # these to their original local names so the tool bodies are copied verbatim.
    ctx = SimpleNamespace(
        readonly=_readonly,
        allow_llm=_allow_llm,
        allow_jira_sync=_allow_jira_sync,
        cap_workflow_payload=_cap_workflow_payload,
        dump=_dump,
        MODE_CAPS=MODE_CAPS,
        Mode=Mode,
        logger=logger,
    )

    # Registration order matches the original in-line definition order (reads, then
    # the always-registered LLM tools, then the READONLY-gated writes).
    register_read_tools(mcp, ctx)
    register_llm_tools(mcp, ctx)
    register_write_tools(mcp, ctx)
    return mcp


def main() -> None:
    # ``rebar-mcp`` takes no options — it speaks MCP-over-stdio. Respond to
    # ``--help`` / ``-h`` with a short usage and exit 0 instead of starting the
    # stdio server (so a curious `rebar-mcp --help` does not hang waiting on stdin,
    # and a CI boot check can confirm the entry point resolves).
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        # Env list is DERIVED from MCP_ENV_VARS so --help can't drift from the
        # manifest (server.json) or the real gates.
        env_lines = "\n".join(
            f"       {v['name']}{'  (deprecated alias)' if v['deprecated'] else ''}"
            for v in MCP_ENV_VARS
        )
        print(  # noqa: T201 — --help output belongs on stdout (server not yet started)
            "rebar-mcp — the rebar MCP server (FastMCP, stdio transport).\n"
            "Usage: rebar-mcp            # serve MCP over stdio (takes no options)\n"
            "Env:\n" + env_lines
        )
        return
    # Observability floor: install a stderr handler on the ``rebar`` root logger so
    # swallowed failures surface. Never stdout — MCP-over-stdio reserves stdout for
    # JSON-RPC framing. See ``rebar._logging`` for the convention.
    from rebar._logging import install_stderr_handler

    install_stderr_handler("rebar")

    # Best-effort ensure-sweep at boot (epic odd-vortex-elbow / WS3): converge a store
    # that is behind the idempotent registry. run_ensures acquires + RELEASES its own
    # store write lock internally (a SHORT budget so a contended lock skips rather
    # than delays boot) — it is NOT held across build_server().run(), which runs under
    # no lock. Log-and-continue: a missing store / import / sweep error never aborts boot.
    try:
        import os

        from rebar import config as _config
        from rebar._store import ensures as _ensures

        _tracker = str(_config.tracker_dir())
        if os.path.isdir(_tracker):
            _ensures.run_ensures(_tracker, timeout=5, attempts=1)
    except Exception:  # noqa: BLE001 — boot must never abort on the ensure sweep
        logging.getLogger("rebar").debug("startup ensure-sweep skipped", exc_info=True)

    build_server().run()


if __name__ == "__main__":
    main()
