"""rebar library — out-of-core engine operations (workflow runs and bridge audit).

The wrappers that reach beyond the plain in-process ticket store: the
workflow-engine entrypoints (``run_workflow`` / ``get_workflow_status`` /
``get_workflow_result``, epic a88f), the explicit bridge operations, and the
``bridge_fsck`` mapping audit — split out of the ``rebar`` package facade
(``__init__.py``, ticket S3 / 4532) so it stays a thin re-export namespace. Every
function is re-exported as ``rebar.<name>``; the three workflow entrypoints are
public attributes but (as before) are deliberately NOT listed in ``rebar.__all__``.
"""

from __future__ import annotations

import datetime
import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rebar import config
from rebar._engine import engine_dir
from rebar._errors import RebarError

if TYPE_CHECKING:
    # Schema-derived return types (story 3a10). Import-only under TYPE_CHECKING.
    from rebar.types import (
        BridgeAccessCheck,
        BridgeControl,
        BridgeFsck,
        BridgeRun,
        BridgeStatus,
        WorkflowRun,
    )


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
    """Offline bridge audit: unknown types, binding drift, and store integrity.
    Anomaly findings are normal results, not errors. Operational scan failures
    raise ``RebarError`` with ``returncode=2``.

    In-process (Tier E E6.5a): runs the audit via ``rebar._engine_support.
    bridge_fsck.audit_bridge_mappings`` instead of subprocessing the dispatcher.
    """
    from pathlib import Path

    from rebar._engine_support.bridge_fsck import audit_bridge_mappings
    from rebar._operation_config import compose_and_bind_operation_snapshot

    with compose_and_bind_operation_snapshot(repo_root=repo_root):
        tracker = config.tracker_dir(repo_root)
        findings = audit_bridge_mappings(Path(tracker))
        return cast("BridgeFsck", findings)


def bridge_projects_list(*, repo_root=None) -> dict:
    """Return the store's bridge-projects mapping ``{key: {"repos": [...]}}``."""
    from rebar._operation_config import compose_and_bind_operation_snapshot

    with compose_and_bind_operation_snapshot(repo_root=repo_root):
        store = _engine_module("rebar_reconciler.projects_store")
        root = Path(config.repo_root(repo_root))
        return store.read_projects(root)


def bridge_projects_set(key, repos, *, repo_root=None) -> None:
    """Replace ``key``'s repos with ``repos`` in the bridge-projects mapping.

    A syntactically-invalid ``key`` (rejected by ``set_project``) is mapped to a
    ``RebarError`` (returncode 2) — the same clean library-error contract the CLI/MCP
    consumers already handle for ``remove``'s absent-key ``KeyError`` — and no lock is
    held for a commit/publish, so nothing is persisted or pushed.
    """
    from rebar._operation_config import compose_and_bind_operation_snapshot
    from rebar._store import lock, push

    store = _engine_module("rebar_reconciler.projects_store")
    root = Path(config.repo_root(repo_root))
    tracker = config.tracker_dir(repo_root)
    with compose_and_bind_operation_snapshot(repo_root=repo_root):
        try:
            with lock.write_lock(tracker):
                store.set_project(root, key, list(repos))
        except ValueError as exc:
            raise _invalid_bridge(str(exc)) from exc
        push.commit_and_push_tickets_branch(tracker, message="bridge: update projects mapping")


def bridge_projects_remove(key, *, repo_root=None) -> None:
    """Remove ``key`` from the bridge-projects mapping.

    Raise ``RebarError`` (naming the key) if it is not present, and likewise map a
    syntactically-invalid ``key`` (rejected by ``remove_project``) to a ``RebarError``
    (returncode 2) so the CLI/MCP consumers get one clean error contract.
    """
    from rebar._operation_config import compose_and_bind_operation_snapshot
    from rebar._store import lock, push

    store = _engine_module("rebar_reconciler.projects_store")
    root = Path(config.repo_root(repo_root))
    tracker = config.tracker_dir(repo_root)
    with compose_and_bind_operation_snapshot(repo_root=repo_root):
        try:
            with lock.write_lock(tracker):
                store.remove_project(root, key)
        except KeyError as exc:
            message = f"bridge project {key!r} is not in the mapping"
            raise RebarError(message, returncode=2, stderr=message) from exc
        except ValueError as exc:
            raise _invalid_bridge(str(exc)) from exc
        push.commit_and_push_tickets_branch(tracker, message="bridge: update projects mapping")


def _engine_module(module_name: str):
    """Import one embedded reconciler module under its supported package name."""
    package = "rebar_reconciler"
    if package not in sys.modules:
        package_dir = engine_dir() / package
        spec = importlib.util.spec_from_file_location(
            package, package_dir / "__init__.py", submodule_search_locations=[str(package_dir)]
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
    return importlib.import_module(module_name)


def _invalid_bridge(message: str) -> RebarError:
    return RebarError(message, returncode=2, stderr=message)


def _validate_positive(name: str, value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
        raise ValueError(f"{name} must be a positive integer")


def _validate_selection_args(only: list[str] | None, exclude: list[str] | None) -> None:
    """Reject malformed local selection arguments before repository discovery."""
    if only is not None and exclude is not None:
        raise ValueError("only and exclude are mutually exclusive")
    selected = only if only is not None else exclude
    if selected is not None and (
        not selected or any(not isinstance(item, str) or not item.strip() for item in selected)
    ):
        raise ValueError("only/exclude must contain non-empty identifiers")


def _bridge_selection(
    root: Path, only: list[str] | None, exclude: list[str] | None
) -> tuple[str | None, set[str] | None]:
    _validate_selection_args(only, exclude)
    selected = only if only is not None else exclude
    if selected is None:
        return None, None
    # resolve_selection/SelectionError moved to pass_support.py (ticket
    # piscine-bullish-cowbird, reconcile_helpers.py module-size headroom).
    helpers = _engine_module("rebar_reconciler.pass_support")
    try:
        resolved = helpers.resolve_selection(root, tuple(item.strip() for item in selected))
    except helpers.SelectionError as exc:
        raise _invalid_bridge(str(exc)) from exc
    return ("only" if only is not None else "except"), resolved


def _bridge_run(
    route: str,
    *,
    only: list[str] | None,
    exclude: list[str] | None,
    max_changes: int | None,
    repo_root,
) -> BridgeRun:
    _validate_positive("max_changes", max_changes)
    _validate_selection_args(only, exclude)
    from rebar._operation_config import compose_and_bind_operation_snapshot

    with compose_and_bind_operation_snapshot(repo_root=repo_root):
        root = Path(config.repo_root(repo_root))
        selection_kind, selection_ids = _bridge_selection(root, only, exclude)
        orchestrator = _engine_module("rebar_reconciler.__main__")
        mode_mod = _engine_module("rebar_reconciler.mode")
        result = orchestrator.run_pass_result(
            repo_root=root,
            target_mode=mode_mod.Mode.DRY_RUN if route == "preview" else mode_mod.Mode.LIVE,
            selection_kind=selection_kind,
            selection_ids=selection_ids,
            max_changes=max_changes,
            route=route,
        )
        returncode = result.disposition.canonical_exit
        payload = {
            "route": route,
            "state": result.disposition.state,
            "returncode": returncode,
            "details": result.details,
        }
        if returncode:
            message = result.canonical_message or result.legacy_message or f"bridge {route} failed"
            raise RebarError(message, returncode=returncode, stderr=message)
        return cast("BridgeRun", payload)


def bridge_preview(
    *,
    only: list[str] | None = None,
    exclude: list[str] | None = None,
    repo_root=None,
) -> BridgeRun:
    """Compute proposed Jira changes without mutating Jira or bridge state."""
    return _bridge_run("preview", only=only, exclude=exclude, max_changes=None, repo_root=repo_root)


def bridge_run(profile: str | None = None, *, repo_root=None) -> BridgeRun:
    """Run one scheduled bridge profile with captured output and strict delivery."""
    from rebar._bridge_runner import run_bridge
    from rebar._operation_config import compose_and_bind_operation_snapshot

    with compose_and_bind_operation_snapshot(repo_root=repo_root):
        return run_bridge(profile, repo_root=repo_root)


def bridge_sync(
    *,
    only: list[str] | None = None,
    exclude: list[str] | None = None,
    max_changes: int | None = None,
    repo_root=None,
) -> BridgeRun:
    """Apply proposed Jira changes, optionally limiting the applied plan."""
    return _bridge_run(
        "sync", only=only, exclude=exclude, max_changes=max_changes, repo_root=repo_root
    )


def bridge_status(
    *,
    target_environment_id: str | None = None,
    max_age_seconds: int | None = None,
    repo_root=None,
) -> BridgeStatus:
    """Read the durable last-pass, pause, and live-lock status snapshot."""
    _validate_positive("max_age_seconds", max_age_seconds)
    from rebar._operation_config import compose_and_bind_operation_snapshot

    with compose_and_bind_operation_snapshot(repo_root=repo_root):
        root = Path(config.repo_root(repo_root))
        last_pass = _engine_module("rebar_reconciler.last_pass")
        try:
            result = last_pass.snapshot(
                root,
                target_environment_id=target_environment_id,
                max_age_seconds=max_age_seconds,
            )
        except Exception as exc:
            raise RebarError(f"cannot read bridge status: {exc}", stderr=str(exc)) from exc
        return cast("BridgeStatus", result)


def _bridge_remote(root: Path) -> str:
    remote = config.tickets_remote(root)
    completed = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", remote],
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise RebarError(f"bridge control requires configured remote {remote!r}")
    return remote


def bridge_pause(reason: str, *, repo_root=None) -> BridgeControl:
    """Persist an idempotent reconciliation pause through the shared CAS ref."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("pause reason must be a non-empty string")
    root = Path(config.repo_root(repo_root))
    remote = _bridge_remote(root)
    from rebar._commands.identity import _git_email

    who = _git_email(str(root))
    if who is None:
        raise RebarError("bridge pause requires a configured git user.email")
    paused_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    ref_lock = _engine_module("rebar_reconciler._ref_lock")
    try:
        ref_lock.set_pause(
            root,
            reason=reason,
            who=who,
            paused_at=paused_at.isoformat().replace("+00:00", "Z"),
            remote=remote,
        )
        pause = ref_lock.read_pause(root, remote=remote)
    except (ref_lock.RefLockError, ref_lock.RefLockTimeoutError) as exc:
        raise RebarError(f"cannot pause bridge: {exc}", stderr=str(exc)) from exc
    assert pause is not None
    return cast(
        "BridgeControl",
        {"state": "paused", **{key: pause[key] for key in ("reason", "who", "paused_at")}},
    )


def bridge_resume(*, repo_root=None) -> BridgeControl:
    """Clear the reconciliation pause through its observed-OID CAS operation."""
    result, _changed = _bridge_resume_operation(repo_root=repo_root)
    return result


def _bridge_resume_operation(*, repo_root=None) -> tuple[BridgeControl, bool]:
    """Internal resume result plus whether an active gate was actually cleared."""
    root = Path(config.repo_root(repo_root))
    remote = _bridge_remote(root)
    ref_lock = _engine_module("rebar_reconciler._ref_lock")
    try:
        changed = ref_lock.clear_gate(root, remote=remote)
    except (ref_lock.RefLockError, ref_lock.RefLockTimeoutError) as exc:
        raise RebarError(f"cannot resume bridge: {exc}", stderr=str(exc)) from exc
    return cast("BridgeControl", {"state": "resumed"}), changed


def bridge_check_access() -> BridgeAccessCheck:
    """Run the live six-step Jira capability check and return its typed verdict."""
    access_check = _engine_module("rebar_reconciler.access_check")
    result, _lines, returncode = access_check.run_access_check()
    if returncode == 2:
        message = "bridge access check requires JIRA_URL, JIRA_USER, and JIRA_API_TOKEN"
        raise RebarError(message, returncode=2, stderr=message)
    return cast("BridgeAccessCheck", result)
