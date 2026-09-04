"""Terraform structural grounding ↔ plan-review integration seam (REB-640).

The plan-review overlay ``T10`` (infra/IaC) is the ONLY criterion Terraform grounding tools
are routed to. This module is the single, testable place that decides that routing and mints
the per-CALL tool provider the :class:`~rebar.llm.workflow.runs.RunnerAgentStep` seam invokes:

* :func:`is_terraform_criterion` / :func:`criteria_are_terraform` — the routing predicate, so
  a NON-Terraform criterion never sees these tools (the plan is explicit: "Non-Terraform
  criteria must NOT see these tools").
* :func:`terraform_evidence_findings` — the Pass-1 findings whose evidence is Terraform-scoped,
  which :mod:`~rebar.llm.plan_review.workflow_ops` routes to an INDEPENDENT Pass-2 that issues
  its OWN query (it may reuse the immutable parse data but must not accept a Pass-1 receipt as
  verification).
* :func:`build_tool_provider` — a ``tool_provider(ctx) -> (tools, finalize) | None`` closure
  for ``RunnerAgentStep``: it mints a FRESH grounding session per agent call (owning an
  immutable parse cache + query ledger), exposes its two refutation queries as function tools,
  and — on finalize — folds the session's concrete + membership reads into a caller-supplied
  usage sink so they join the signed read-set deterministically
  (:func:`rebar.llm.usage_log.merge_synthetic_reads`).

``hcl2``/``lark`` are never imported here; the grounding session imports them lazily inside the
worker only, so wiring this seam into plan-review does not pull the HCL parser into a
non-Terraform review.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: The one plan-review overlay criterion Terraform grounding is routed to (det_gate_rules T10:
#: the audited infra-vocabulary pre-filter incl. `terraform`). Kept as a module constant so the
#: routing predicate and the Pass-2 router agree on a single id.
TERRAFORM_CRITERION = "T10"


def is_terraform_criterion(criterion_id: str) -> bool:
    """True iff ``criterion_id`` is the Terraform-routed overlay (``T10``)."""
    return criterion_id == TERRAFORM_CRITERION


def criteria_are_terraform(criteria: object) -> bool:
    """True iff ANY id in ``criteria`` is Terraform-routed.

    ``criteria`` is the per-call criterion id list a plan-review chunk carries. Non-iterable or
    empty input is not Terraform-routed (so a call with no criteria never gets the tools)."""
    if not isinstance(criteria, (list, tuple, set, frozenset)):
        return False
    return any(is_terraform_criterion(str(c)) for c in criteria)


def terraform_evidence_findings(findings: object) -> list[dict]:
    """The Pass-1 findings that cite the Terraform criterion — the Pass-2 routing set.

    A finding is Terraform-scoped when its ``criteria`` list includes ``T10``. These are the
    findings :mod:`~rebar.llm.plan_review.workflow_ops` routes to an independent Terraform
    Pass-2. Size-ladder (``_too_big``) and budget-shed (``_shed``) findings are excluded, exactly
    as :func:`~rebar.llm.plan_review.workflow_ops.plan_review_grounding` excludes them, so a shed
    criterion does not trigger a Pass-2 that never ran in Pass-1."""
    out: list[dict] = []
    if not isinstance(findings, (list, tuple)):
        return out
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("_too_big") or finding.get("_shed"):
            continue
        if criteria_are_terraform(finding.get("criteria")):
            out.append(finding)
    return out


def any_terraform_evidence(findings: object) -> bool:
    """True iff any Pass-1 finding is Terraform-scoped (the Pass-2 gate boolean)."""
    return bool(terraform_evidence_findings(findings))


def _criteria_from_ctx(ctx: Any) -> object:
    """Best-effort extraction of the per-call criterion ids from a step context.

    A plan-review agent chunk carries its criterion ids on the step inputs (``criteria`` — the
    single-criterion agentic finder submits ``[c]``); the completion/other steps carry none.
    Reads defensively so a context without the field simply routes to "not Terraform"."""
    inputs = getattr(ctx, "inputs", None)
    if isinstance(inputs, dict):
        for key in ("criteria", "criterion_ids", "probe_criteria"):
            val = inputs.get(key)
            if val:
                return val
    return None


_TF_SUFFIXES = (".tf", ".tf.json")


def _iter_cited_paths(finding: dict) -> list[str]:
    """Every source path a finding cites (its evidence ``location.file`` + any ``files`` list)."""
    out: list[str] = []
    location = finding.get("location")
    if isinstance(location, dict) and isinstance(location.get("file"), str):
        out.append(location["file"])
    for key in ("files", "cited_files", "paths"):
        val = finding.get(key)
        if isinstance(val, list):
            out.extend(p for p in val if isinstance(p, str))
    return out


def selected_from_findings(findings: object) -> list[str]:
    """The sorted, deduped ``.tf``/``.tf.json`` paths cited by Terraform-scoped findings.

    This is the ``selected`` seed a Pass-2 Terraform verification snapshot is built over: the
    verifier issues its OWN query against the captures the Pass-1 findings pointed at (it may
    reuse the immutable parse data but never accepts a Pass-1 receipt as verification). Only
    Terraform-criterion findings contribute, so a mixed review seeds nothing from non-Terraform
    findings."""
    out: set[str] = set()
    for finding in terraform_evidence_findings(findings):
        for path in _iter_cited_paths(finding):
            if any(path.endswith(suffix) for suffix in _TF_SUFFIXES):
                out.add(path)
    return sorted(out)


def _selected_from_ctx(ctx: Any, explicit: list[str] | None) -> list[str]:
    """The ``selected`` seed for a call: the explicit set if given, else the ``.tf`` paths cited
    by the call's Terraform findings (the Pass-2 verify case)."""
    if explicit:
        return explicit
    inputs = getattr(ctx, "inputs", None)
    findings = inputs.get("findings") if isinstance(inputs, dict) else None
    return selected_from_findings(findings)


def _ctx_is_terraform(ctx: Any) -> bool:
    """True iff the call is Terraform-routed — by its criterion ids OR its cited findings."""
    if criteria_are_terraform(_criteria_from_ctx(ctx)):
        return True
    inputs = getattr(ctx, "inputs", None)
    findings = inputs.get("findings") if isinstance(inputs, dict) else None
    return any_terraform_evidence(findings)


def build_tool_provider(
    *,
    repo_root: str,
    selected: list[str] | None = None,
    usage_sink: dict[str, Any],
    force: bool = False,
) -> Callable[[Any], tuple[list, Callable[[], None]] | None]:
    """A per-CALL ``tool_provider`` for ``RunnerAgentStep`` scoped to Terraform criteria.

    Returns a closure ``provider(ctx)`` that, for a Terraform-routed call (``T10`` in the
    call's criteria, a Terraform-scoped finding in its inputs, or ``force=True`` for a dedicated
    Terraform gate step), opens a FRESH
    :class:`~rebar.grounding.terraform_tools.TerraformSession` over ``(repo_root, selected)`` and
    returns ``([lookup_tool, resolve_tool], finalize)``. ``selected`` defaults per-call to the
    ``.tf`` paths cited by the call's Terraform findings (:func:`selected_from_findings`), so the
    Pass-2 verifier re-grounds against exactly what Pass-1 pointed at. The finalizer frees the
    session and merges its concrete + membership reads into ``usage_sink['distinct_fetches']`` via
    :func:`rebar.llm.usage_log.merge_synthetic_reads`, so a session that reads files in-process
    (never through the agent's ``read_file`` tool) still contributes to the signed read-set.

    A non-Terraform call returns ``None`` (the step keeps only its static ``extra_tools``). When
    the ``grounding-terraform`` extra is absent the session still opens — its queries return a
    closed ``no_tool``/``missing_extra`` abstention rather than raising — so routing is
    unconditional and the tools' behaviour degrades, matching the plan's fail-open contract."""

    def provider(ctx: Any) -> tuple[list, Callable[[], None]] | None:
        if not (force or _ctx_is_terraform(ctx)):
            return None
        call_selected = selected if force else _selected_from_ctx(ctx, selected)
        return _open_session_tools(
            repo_root=repo_root, selected=call_selected or [], usage_sink=usage_sink
        )

    return provider


def pass1_tool_hook(
    *,
    repo_root: str,
    selected: list[str] | None = None,
    usage_sink: dict[str, Any],
) -> Callable[[list[str], bool], tuple[list, Callable[[], None]] | None]:
    """The Pass-1 finder tool hook for the PRODUCTION plan-review path (REB-640 AC6/AC7).

    Returns ``hook(criteria_ids, agentic) -> (tools, finalize) | None`` that
    :func:`rebar.llm.plan_review.passes.pass1_chunk` consults per call and rides onto the
    call's ``RunRequest.extra_tools``. It mints a session — the two refutation tools plus a
    finalizer that folds the session's concrete + membership reads into ``usage_sink`` — ONLY
    when the call is a Terraform-routed (``T10``) AGENTIC finder. For a non-T10 chunk OR any
    single-turn call it returns ``None``, so those calls carry NO Terraform tools and stay
    byte-identical to today (the plan: "Non-Terraform criteria must NOT see these tools").

    This is the seam ``pass1_chunk``/``pass1_with_ladder``/``run_pass1`` thread through so the
    tools reach the DISCARDED-``agent_runner`` production finder that
    :class:`~rebar.llm.plan_review.production_batch_runner.ProductionBatchRunner` drives — the
    ``RunnerAgentStep.tool_provider`` seam only reaches the agentic Pass-2 verifier."""

    def hook(criteria_ids: list[str], agentic: bool) -> tuple[list, Callable[[], None]] | None:
        if not agentic or not criteria_are_terraform(criteria_ids):
            return None
        return _open_session_tools(
            repo_root=repo_root, selected=list(selected or []), usage_sink=usage_sink
        )

    return hook


def _open_session_tools(
    *, repo_root: str, selected: list[str], usage_sink: dict[str, Any]
) -> tuple[list, Callable[[], None]]:
    """Mint a fresh grounding session's ``(tools, finalize)`` over ``(repo_root, selected)``.

    The single session-lifecycle place shared by :func:`build_tool_provider` (Pass-2) and
    :func:`pass1_tool_hook` (Pass-1): open the per-call session (fail-open when the extra is
    absent), expose its two refutation queries, and — on finalize — fold its concrete +
    membership reads into ``usage_sink`` so in-process reads join the signed read-set."""
    from rebar.grounding import terraform_tools as tft

    session = tft.open_session(repo_root=repo_root, selected=selected)
    tools = _session_tools(session)

    def finalize() -> None:
        _fold_usage(usage_sink, session.finalize())

    return tools, finalize


def _session_tools(session: Any) -> list:
    """The two refutation queries of a session exposed as agent function tools.

    Each is a thin, self-documenting callable returning the query's grounding evidence +
    canonical receipt; the runner wraps a plain callable as a function tool. Only refutation
    queries are exposed — the session NEVER emits ``match`` and NEVER asserts an absence."""

    def terraform_lookup_declaration(address: str, module_path: str = "") -> dict:
        """Refute an asserted ABSENCE of a Terraform declaration by structural address.

        ``address`` is a canonical Terraform address (e.g. ``variable.region``,
        ``aws_instance.web``, ``data.aws_ami.base``, ``module.vpc``). Returns
        ``{evidence, receipt}``: ``refuted`` when a real declaration disproves the absence
        (with its source span), else a closed ``abstain`` reason. NEVER confirms presence."""
        result = session.lookup_declaration(address, module_path=module_path)
        return {"evidence": result.evidence, "receipt": result.receipt}

    def terraform_resolve_reference(reference: str, from_file: str) -> dict:
        """Refute an asserted ABSENCE of a referenced Terraform member/output.

        ``reference`` is a member reference (e.g. ``var.region``, ``module.vpc.vpc_id``);
        ``from_file`` is the repo-relative ``.tf`` it appears in. Returns ``{evidence,
        receipt}`` with the same three-valued (``refuted``/``abstain``) contract."""
        result = session.resolve_reference(reference, from_file=from_file)
        return {"evidence": result.evidence, "receipt": result.receipt}

    return [terraform_lookup_declaration, terraform_resolve_reference]


def _fold_usage(usage_sink: dict[str, Any], usage: Any) -> None:
    """Merge a finalized session :class:`Usage` into ``usage_sink['distinct_fetches']``.

    Deterministic and idempotent-per-target: :func:`merge_synthetic_reads` dedupes on
    ``(tool, target)`` and sorts the additions, so folding multiple sessions (or the same
    reads twice) yields a stable read-set regardless of query/session order."""
    from rebar.llm.usage_log import merge_synthetic_reads

    fetches = usage_sink.get("distinct_fetches")
    if not isinstance(fetches, list):
        fetches = []
    usage_sink["distinct_fetches"] = merge_synthetic_reads(
        fetches,
        concrete_reads=getattr(usage, "concrete_reads", ()) or (),
        membership_globs=getattr(usage, "membership_globs", ()) or (),
    )
