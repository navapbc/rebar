"""Library-facing run orchestration + status/result reads (WS-C4).

The single place that turns "a workflow source + inputs" into a persisted run and
reads its state back via replay, shared by the library facade
(``rebar.run_workflow`` / ``get_workflow_status`` / ``get_workflow_result``), the
CLI (``rebar workflow run/status/result``), and the MCP tools. Keeping it here
keeps the heavy logic out of the already-large ``rebar`` facade and the thin
executor.

A run targets a ticket (run-state lives on that ticket's event log, WS-C1), so a
small **local, git-ignored** index under ``.rebar/workflow_runs/<run_id>`` maps a
run_id back to its ticket — mirroring the ``.rebar/current_session_log`` pointer —
so ``status``/``result`` can be looked up by run_id alone.

Agent steps run through :class:`RunnerAgentStep`, which bridges an executor agent
step to the rebar.llm review Runner stack (WS-K2): a real (config-selected) runner
for a live run, an injected runner for the parallel-diff/tests, and the offline
FakeAgentRunner for ``dry_run=True`` (no tokens). Scripted steps always run for real.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rebar.llm.errors import WorkflowError, WorkflowParseError

from . import executor as _ex
from . import steps as _steps  # noqa: F401 — importing registers the built-in scripted steps
from .migrate import migrate_to_current
from .schema import load_workflow as _load_file


def _repo_root(repo_root: str | None) -> Path:
    if repo_root:
        return Path(repo_root)
    from rebar import config

    return Path(config.repo_root())


def _index_dir(repo_root: str | None) -> Path:
    return _repo_root(repo_root) / ".rebar" / "workflow_runs"


def record_run_location(run_id: str, ticket_id: str, repo_root: str | None) -> None:
    d = _index_dir(repo_root)
    d.mkdir(parents=True, exist_ok=True)
    (d / run_id).write_text(ticket_id, encoding="utf-8")


def lookup_run_location(run_id: str, repo_root: str | None) -> str | None:
    f = _index_dir(repo_root) / run_id
    try:
        return f.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _resolve_source_path(source: str, repo_root: str | None) -> Path:
    p = Path(source)
    if p.exists():
        return p
    cand = _repo_root(repo_root) / ".rebar" / "workflows" / f"{source}.yaml"
    if cand.exists():
        return cand
    raise WorkflowParseError(
        f"workflow {source!r} not found (no such file, and no .rebar/workflows/{source}.yaml)",
        source=str(source),
    )


def load_workflow_doc(source: str | Path | dict, repo_root: str | None = None) -> dict[str, Any]:
    """Resolve ``source`` (a dict, a file path, or a workflow name) to a parsed,
    migrated workflow document."""
    if isinstance(source, dict):
        doc = dict(source)
    else:
        path = _resolve_source_path(str(source), repo_root)
        doc = _load_file(path)
    return migrate_to_current(doc, source=str(source))


class RunnerAgentStep(_ex.AgentStepRunner):
    """Bridge an executor AGENT step to the rebar.llm review Runner stack (WS-K2).

    A workflow agent step runs a real (or injected) tool-using agent and returns its
    finalized result as the step's outputs. The step's ``prompt:`` is a reviewer id
    (resolved via the prompt library → system prompt); ``model:`` follows the WS-D3
    precedence (step > workflow > config > env > default); ``mode``/``output_schema``
    flow into the RunRequest (the WS-D1 generalized contract). An injected ``runner``
    (e.g. FakeRunner) is the offline/parallel-diff seam; otherwise the config-selected
    runner is used (langgraph; a missing extra/key fails cleanly via get_runner)."""

    def __init__(self, *, runner=None, repo_root: str | None = None) -> None:
        self._runner = runner
        self._repo_root = repo_root

    def run(self, ctx: _ex.StepContext) -> _ex.StepResult:
        from dataclasses import replace as _replace

        from rebar.llm import prompts
        from rebar.llm.config import LLMConfig, resolve_model
        from rebar.llm.runner import RunRequest, get_runner

        cfg = LLMConfig.from_env(repo_root=self._repo_root)
        cfg = _replace(
            cfg,
            model=resolve_model(
                cfg, step=ctx.step.get("model"), workflow=ctx.workflow.get("model")
            ),
        )
        prompt_id = ctx.step.get("prompt") or ""
        reviewer = prompts.get_reviewer(prompt_id)
        ticket_id = str(ctx.inputs.get("ticket_id") or ctx.target_ticket or "")
        variables = {
            "ticket_id": ticket_id,
            "ticket_context": str(
                ctx.inputs.get("context") or ctx.inputs.get("ticket_context") or ""
            ),
            "repo_path": cfg.repo_path or "",
        }
        system_prompt, langfuse_prompt = prompts.resolve_prompt(reviewer, variables, cfg.langfuse)
        instructions = str(
            ctx.inputs.get("instructions")
            or f"Review along the '{reviewer.dimension}' dimension using the read-only "
            "repository tools; ground every finding in tool output."
        )
        req = RunRequest(
            system_prompt=system_prompt,
            instructions=instructions,
            config=cfg,
            reviewers=[prompt_id],
            target={"kind": "workflow_step", "ticket_ids": [ticket_id] if ticket_id else []},
            langfuse_prompt=langfuse_prompt,
            mode=ctx.step.get("mode", "findings"),
            output_schema=ctx.step.get("output_schema"),
        )
        return _ex.StepResult(outputs=get_runner(cfg, override=self._runner).run(req))


def _agent_runner(
    dry_run: bool, *, repo_root: str | None = None, review_runner=None
) -> _ex.AgentStepRunner:
    """Select the agent-step runner: an injected review runner (parallel-diff /
    tests), the offline FakeAgentRunner for ``dry_run``, else the real
    RunnerAgentStep bridge (config-selected review runner)."""
    if review_runner is not None:
        return RunnerAgentStep(runner=review_runner, repo_root=repo_root)
    if dry_run:
        return _ex.FakeAgentRunner()
    return RunnerAgentStep(repo_root=repo_root)


def run(
    source: str | Path | dict,
    inputs: dict[str, Any] | None = None,
    *,
    ticket_id: str | None = None,
    run_id: str | None = None,
    dry_run: bool = False,
    repo_root: str | None = None,
    secrets: dict[str, str] | None = None,
    review_runner=None,
) -> dict[str, Any]:
    """Execute a workflow and return its result as a dict (WS-C4 sync entrypoint).

    Persists run-state to ``ticket_id`` (durable; resumable) when given, and records
    the run_id→ticket index so status/result resolve by run_id. Sweeps orphaned
    snapshots first (WS-C3 backstop). Synchronous — the MCP layer wraps this for its
    async, return-run_id-immediately contract. ``review_runner`` injects a specific
    rebar.llm Runner into agent steps (the offline/parallel-diff seam)."""
    doc = load_workflow_doc(source, repo_root)
    run_id = run_id or _ex.new_run_id()
    _ex.sweep_orphan_snapshots(repo_root)
    if ticket_id:
        record_run_location(run_id, ticket_id, repo_root)
    res = _ex.run_workflow(
        doc,
        inputs,
        run_id=run_id,
        target_ticket=ticket_id,
        repo_root=repo_root,
        agent_runner=_agent_runner(dry_run, repo_root=repo_root, review_runner=review_runner),
        secrets=secrets,
    )
    return _result_dict(res, ticket_id, dry_run)


def _result_dict(res: _ex.RunResult, ticket_id: str | None, dry_run: bool) -> dict[str, Any]:
    return {
        "run_id": res.run_id,
        "ticket_id": ticket_id,
        "workflow_name": res.workflow_name,
        "status": res.status,
        "dry_run": dry_run,
        "terminal_step": res.terminal_step,
        "terminal_output": res.terminal_output,
        "outputs": dict(res.outputs),
        "steps": dict(res.steps),
        "error": res.error,
    }


def _reduce_ticket_state(ticket_id: str, repo_root: str | None) -> dict[str, Any]:
    from rebar import _reads

    return _reads.show_ticket(ticket_id, repo_root=repo_root)


def _locate(run_id: str, ticket_id: str | None, repo_root: str | None) -> str:
    tid = ticket_id or lookup_run_location(run_id, repo_root)
    if not tid:
        raise WorkflowError(f"unknown run_id {run_id!r}: no run-index entry and no ticket_id given")
    return tid


def status(
    run_id: str, ticket_id: str | None = None, *, repo_root: str | None = None
) -> dict[str, Any]:
    """Read a run's current status via replay (no execution)."""
    tid = _locate(run_id, ticket_id, repo_root)
    state = _reduce_ticket_state(tid, repo_root)
    run = state.get("workflow_runs", {}).get(run_id)
    if run is None:
        raise WorkflowError(f"run {run_id!r} not found on ticket {tid}")
    steps = state.get("workflow_steps", {}).get(run_id, {})
    return {
        "run_id": run_id,
        "ticket_id": tid,
        "workflow_name": run.get("workflow_name"),
        "status": run.get("status"),
        "terminal_step": run.get("terminal_step"),
        "error": run.get("error"),
        "steps": {sid: s.get("status") for sid, s in steps.items()},
    }


def result(
    run_id: str, ticket_id: str | None = None, *, repo_root: str | None = None
) -> dict[str, Any]:
    """Read a run's outputs (the terminal step's output is the run result)."""
    tid = _locate(run_id, ticket_id, repo_root)
    state = _reduce_ticket_state(tid, repo_root)
    run = state.get("workflow_runs", {}).get(run_id)
    if run is None:
        raise WorkflowError(f"run {run_id!r} not found on ticket {tid}")
    steps = state.get("workflow_steps", {}).get(run_id, {})
    terminal = run.get("terminal_step")
    terminal_output = steps.get(terminal, {}).get("outputs") if terminal else None
    return {
        "run_id": run_id,
        "ticket_id": tid,
        "workflow_name": run.get("workflow_name"),
        "status": run.get("status"),
        "terminal_step": terminal,
        "terminal_output": terminal_output,
        "outputs": {sid: s.get("outputs", {}) for sid, s in steps.items()},
        "error": run.get("error"),
    }
