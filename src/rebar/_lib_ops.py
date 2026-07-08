"""rebar library — out-of-core engine operations (workflow runs, Jira reconcile,
bridge-mapping audit).

The wrappers that reach beyond the plain in-process ticket store: the
workflow-engine entrypoints (``run_workflow`` / ``get_workflow_status`` /
``get_workflow_result``, epic a88f), the Jira ``reconcile`` subprocess launcher,
and the ``bridge_fsck`` mapping audit — split out of the ``rebar`` package facade
(``__init__.py``, ticket S3 / 4532) so it stays a thin re-export namespace. Every
function is re-exported as ``rebar.<name>``; the three workflow entrypoints are
public attributes but (as before) are deliberately NOT listed in ``rebar.__all__``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING, cast

from rebar import config
from rebar._engine import engine_env
from rebar._errors import RebarError

if TYPE_CHECKING:
    # Schema-derived return types (story 3a10). Import-only under TYPE_CHECKING.
    from rebar.types import BridgeFsck, WorkflowRun


# ── Workflow engine (epic a88f) — sync library entrypoints (WS-C4) ────────────
def run_workflow(
    source,
    inputs: dict | None = None,
    *,
    ticket_id: str | None = None,
    run_id: str | None = None,
    dry_run: bool = False,
    repo_root=None,
    secrets: dict | None = None,
) -> dict:
    """Run a workflow (a ``.rebar/workflows/<name>.yaml`` path/name or a dict) and
    return its result. Synchronous; persists run-state to ``ticket_id`` when given.
    ``dry_run=True`` executes agent steps with the offline FakeRunner (no tokens).
    See :mod:`rebar.llm.workflow.runs`."""
    from rebar.llm.workflow import runs

    return runs.run(
        source,
        inputs,
        ticket_id=ticket_id,
        run_id=run_id,
        dry_run=dry_run,
        repo_root=repo_root,
        secrets=secrets,
    )


def get_workflow_status(
    run_id: str, ticket_id: str | None = None, *, repo_root=None
) -> WorkflowRun:
    """A workflow run's current status, read via replay (no execution)."""
    from rebar.llm.workflow import runs

    return cast("WorkflowRun", runs.status(run_id, ticket_id, repo_root=repo_root))


def get_workflow_result(
    run_id: str, ticket_id: str | None = None, *, repo_root=None
) -> WorkflowRun:
    """A workflow run's outputs (the terminal step's output is the result)."""
    from rebar.llm.workflow import runs

    return cast("WorkflowRun", runs.result(run_id, ticket_id, repo_root=repo_root))


def bridge_fsck(*, repo_root=None) -> BridgeFsck:
    """Bridge-mapping audit as structured JSON: {orphaned, duplicates, stale}.
    A nonzero exit (anomalies present) is NORMAL, not an error.

    In-process (Tier E E6.5a): runs the audit via ``rebar._engine_support.
    bridge_fsck.audit_bridge_mappings`` instead of subprocessing the dispatcher.
    """
    from pathlib import Path

    from rebar._engine_support.bridge_fsck import audit_bridge_mappings

    tracker = config.tracker_dir(repo_root)
    findings = audit_bridge_mappings(Path(tracker))
    return cast("BridgeFsck", {k: findings.get(k, []) for k in ("orphaned", "duplicates", "stale")})


# ── Reconciler (Jira sync) ────────────────────────────────────────────────────
def reconcile(mode: str = "dry-run", *, repo_root=None) -> dict:
    """Run the Jira reconciler. Defaults to a non-mutating ``dry-run``.

    Modes: reconcile-check | dry-run | bootstrap-strict | bootstrap-throttle | live.
    The Jira-mutating modes are ``bootstrap-strict``, ``bootstrap-throttle`` and
    ``live`` (each requires the ``acli`` binary + credentials); ``reconcile-check``
    and ``dry-run`` are non-mutating.
    """
    root = str(config.repo_root(repo_root))
    # Launch under THIS interpreter (sys.executable), not a bare ``python3``: Tier E
    # E5b rewired the reconciler onto in-package ``rebar.*`` imports, so it must run
    # on the rebar-capable interpreter. engine_env still puts the engine dir on
    # PYTHONPATH so the top-level ``rebar_reconciler`` package resolves.
    cmd = [
        sys.executable,
        "-m",
        "rebar_reconciler",
        "--mode",
        mode,
        "--repo-root",
        root,
    ]
    cp = subprocess.run(cmd, env=engine_env(root), text=True, capture_output=True, check=False)
    if cp.returncode not in (0, 75):  # 75 == EXIT_RESCHEDULE
        raise RebarError(
            f"reconcile ({mode}) failed (exit {cp.returncode}): {cp.stderr.strip()}",
            returncode=cp.returncode,
            stderr=cp.stderr,
        )
    out = cp.stdout.strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # No-write modes (dry-run / reconcile-check) emit the computed plan as
        # a JSON object on the FINAL stdout line; any preceding diagnostic
        # lines are informational. Fall back to parsing the last line so the
        # plan still reaches the caller (ticket yaw-plait-doe).
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if lines:
            try:
                return json.loads(lines[-1])
            except json.JSONDecodeError:
                pass
        return {"mode": mode, "returncode": cp.returncode, "output": out, "stderr": cp.stderr}
