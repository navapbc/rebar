"""The scripted-step library: deterministic built-in steps (WS-E).

Scripted steps are the pure-Python building blocks a workflow wires together with
``uses:``. The registry + step interface (``register_step`` / ``StepContext`` /
``StepResult``) live in :mod:`rebar.llm.workflow.executor`; importing THIS module
registers the built-ins into that registry (the entrypoints in
:mod:`rebar.llm.workflow.runs` import it, so they are always available there).

Built-ins:
  * ``fetch_ticket``      — compiled ticket state for the target/`ticket_id`.
  * ``fetch_commits``     — commit SHAs attached to a ticket (WS-H surface).
  * ``fetch_epic_graph``  — a ticket's dependency/child graph.
  * ``render_context``    — assemble a readable text context from inputs.
  * ``gate``              — evaluate findings against a VERSIONED policy (UNSECURED
    here; cryptographic gate security/signing is epic 8a1c).
  * ``comment_verdict``   — post a verdict comment (idempotent on (run_id,step_id)).
  * ``tag``               — add a tag.
  * ``set_fields``        — edit ticket fields.

Side-effecting steps (``comment_verdict``/``tag``/``set_fields``) are idempotent:
the executor skips a step whose marker is already committed (WS-C3), and the
comment additionally embeds the ``(run_id, step_id)`` token so a crash-and-resume
never double-comments. ``rebar`` is imported lazily inside each step so importing
this module only runs the registration decorators (no import cycle, no heavy load).
"""

from __future__ import annotations

from typing import Any

from .executor import StepContext, StepResult, register_step

__all__ = ["GATE_POLICIES"]


def _ticket_id(ctx: StepContext) -> str:
    tid = ctx.inputs.get("ticket_id") or ctx.target_ticket
    if not tid:
        raise ValueError(
            f"step {ctx.step_id!r} needs a ticket: pass `with: {{ticket_id: ...}}` or run "
            f"the workflow against a target ticket"
        )
    return str(tid)


# ── read steps ────────────────────────────────────────────────────────────────


@register_step("fetch_ticket")
def fetch_ticket(ctx: StepContext) -> dict[str, Any]:
    """Compiled ticket state for the target ticket → {ticket, title, description, …}."""
    import rebar

    tid = _ticket_id(ctx)
    state = rebar.show_ticket(tid, repo_root=ctx.repo_root)
    return {
        "ticket": state,
        "ticket_id": tid,
        "title": state.get("title"),
        "description": state.get("description"),
        "status": state.get("status"),
        "ticket_type": state.get("ticket_type"),
        "tags": state.get("tags", []),
    }


@register_step("fetch_commits")
def fetch_commits(ctx: StepContext) -> dict[str, Any]:
    """Commit SHAs attached to a ticket (the WS-H commits-on-ticket surface).

    Reads the ticket's compiled ``commits`` list, so it works the moment WS-H
    starts populating it and returns ``[]`` (not an error) before then."""
    import rebar

    tid = _ticket_id(ctx)
    state = rebar.show_ticket(tid, repo_root=ctx.repo_root)
    commits = state.get("commits", []) or []
    return {"commits": commits, "commit_count": len(commits)}


@register_step("fetch_epic_graph")
def fetch_epic_graph(ctx: StepContext) -> dict[str, Any]:
    """A ticket's dependency/child graph → {deps, blockers, children}."""
    import rebar

    tid = _ticket_id(ctx)
    graph = rebar.deps(tid, repo_root=ctx.repo_root)
    return {
        "graph": graph,
        "children": graph.get("children", []),
        "blockers": graph.get("blockers", []),
        "deps": graph.get("deps", []),
    }


@register_step("render_context")
def render_context(ctx: StepContext) -> dict[str, Any]:
    """Assemble a readable text context block from the step's inputs.

    Each ``with:`` entry becomes a ``## <key>`` section; structured values are
    rendered as compact JSON. Output ``{context}`` is meant to feed an agent step."""
    import json

    parts: list[str] = []
    for key, value in ctx.inputs.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        if isinstance(value, str):
            rendered = value
        else:
            rendered = json.dumps(value, indent=2, ensure_ascii=False, default=str)
        parts.append(f"## {key}\n{rendered}")
    return {"context": "\n\n".join(parts)}


# ── the gate (unsecured; versioned policy) ────────────────────────────────────

# Versioned gate policies. UNSECURED by design here — this is an ordinary example
# step; cryptographic signing/attestation/protected-tags is epic 8a1c. A policy
# fails the gate when any finding's severity is in ``fail_on_severity`` (or the
# finding count exceeds ``max_findings``).
GATE_POLICIES: dict[str, dict[str, Any]] = {
    "default": {"version": "1", "fail_on_severity": ["critical", "high"], "max_findings": None},
    "strict": {
        "version": "1",
        "fail_on_severity": ["critical", "high", "medium"],
        "max_findings": 0,
    },
    "advisory": {"version": "1", "fail_on_severity": [], "max_findings": None},
}


@register_step("gate")
def gate(ctx: StepContext) -> dict[str, Any]:
    """Evaluate findings against a versioned policy → {verdict, passed, …}.

    Inputs: ``findings`` (list of {severity, …}) and ``policy`` (a name in
    GATE_POLICIES; default ``default``). Deterministic, pure Python."""
    raw = ctx.inputs.get("findings") or []
    if not isinstance(raw, list):
        raw = []
    # Count only well-formed findings; garbage entries neither fail-on-severity nor
    # inflate the max_findings count (so the verdict reason stays meaningful).
    findings = [f for f in raw if isinstance(f, dict)]
    policy_name = ctx.inputs.get("policy") or "default"
    policy = GATE_POLICIES.get(policy_name, GATE_POLICIES["default"])

    failing = [f for f in findings if f.get("severity") in policy["fail_on_severity"]]
    over_count = policy["max_findings"] is not None and len(findings) > policy["max_findings"]
    verdict = "fail" if (failing or over_count) else "pass"
    return {
        "verdict": verdict,
        "passed": verdict == "pass",
        "policy": policy_name,
        "policy_version": policy["version"],
        "failing_count": len(failing),
        "total_findings": len(findings),
    }


# ── side-effecting steps (idempotent on (run_id, step_id)) ────────────────────


@register_step("comment_verdict")
def comment_verdict(ctx: StepContext) -> StepResult:
    """Post a verdict/summary comment to the ticket — idempotent within a run.

    Embeds a ``[rebar-run <run_id>/<step_id>]`` marker and skips if a comment with
    that marker already exists, so a crash-and-resume of the SAME run
    (effect-applied-but-marker-unwritten) never double-comments. A deliberately NEW
    run (fresh run_id) is a new verdict and posts a new comment by design."""
    import rebar

    tid = _ticket_id(ctx)
    marker = f"[rebar-run {ctx.run_id}/{ctx.step_id}]"
    state = rebar.show_ticket(tid, repo_root=ctx.repo_root)
    for c in state.get("comments", []):
        if marker in (c.get("body") or ""):
            return StepResult(
                outputs={"commented": False, "idempotent_skip": True, "marker": marker}
            )

    verdict = ctx.inputs.get("verdict")
    summary = ctx.inputs.get("body") or ctx.inputs.get("summary") or ""
    header = f"**Workflow verdict: {verdict}**\n\n" if verdict else ""
    body = f"{header}{summary}\n\n{marker}".strip()
    rebar.comment(tid, body, repo_root=ctx.repo_root)
    return StepResult(outputs={"commented": True, "marker": marker, "verdict": verdict})


@register_step("tag")
def tag_step(ctx: StepContext) -> dict[str, Any]:
    """Add a tag to the ticket (idempotent: adding an existing tag is a no-op)."""
    import rebar

    tid = _ticket_id(ctx)
    label = ctx.inputs.get("tag")
    if not label:
        raise ValueError(f"step {ctx.step_id!r} (tag) needs `with: {{tag: ...}}`")
    rebar.tag(tid, str(label), repo_root=ctx.repo_root)
    return {"tagged": label}


@register_step("set_fields")
def set_fields(ctx: StepContext) -> dict[str, Any]:
    """Edit ticket fields from ``with: {fields: {name: value, …}}`` (LWW; idempotent
    when the values are unchanged)."""
    import rebar

    tid = _ticket_id(ctx)
    fields = ctx.inputs.get("fields") or {}
    if not isinstance(fields, dict) or not fields:
        raise ValueError(f"step {ctx.step_id!r} (set_fields) needs `with: {{fields: {{...}}}}`")
    rebar.edit_ticket(tid, repo_root=ctx.repo_root, **fields)
    return {"updated": sorted(fields.keys())}
