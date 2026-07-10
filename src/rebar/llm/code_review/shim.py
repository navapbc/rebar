"""The public ``review_code`` surface, gate-backed (epic b744 / WS4).

This REPLACES the retired single-pass route: ``review_code`` keeps its name/signature and its
``review_result`` return shape, but its IMPLEMENTATION is now the four-pass gate.

- **Disabled** (the default — ``verify.enable_code_review`` off): returns a valid EMPTY
  ``review_result`` (zero findings + a 'capability disabled' note), ZERO LLM calls — INERT.
- **Enabled:** assembles the diff context, runs ``produce_code_review_verdict`` (the gate), and
  TRANSLATES the ``code_review_verdict`` → ``review_result`` (reusing ``finalize_findings`` so the
  result is schema-valid). The raw verdict is attached under a ``verdict`` key for callers that
  want the typed gate output (blocking/advisory/coaching/coverage).

The CLI (``rebar review-code``) and MCP (``review_code``) call this unchanged.
"""

from __future__ import annotations

from typing import Any

from rebar.llm.config import LLMConfig

_SEVERITY_DEFAULT = "medium"

# The kernel Pass-3 severity vocabulary ({critical, major, minor, none} from
# review_kernel.severity_label) → the common.finding enum ({critical, high, medium, low, info}).
# WITHOUT this map the raw kernel label flows into findings.normalize_finding, which clamps any
# UNKNOWN severity to "info" — silently flattening every non-critical gate finding to the lowest
# severity and corrupting the severity×agreement ranking. This is the first place a kernel
# verdict is translated to common.finding, so the mismatch surfaces only here.
_KERNEL_TO_COMMON_SEVERITY = {
    "critical": "critical",
    "major": "high",
    "minor": "medium",
    "none": "info",
}


def _parse_location(loc: Any) -> tuple[str | None, int | None]:
    if not isinstance(loc, str) or not loc.strip():
        return (None, None)
    s = loc.strip()
    path, sep, rest = s.rpartition(":")
    if sep and path:
        head = rest.split("-", 1)[0].split(",", 1)[0].strip()
        try:
            return (path, int(head))
        except ValueError:
            return (s, None)
    return (s, None)


def _to_common_finding(f: dict[str, Any]) -> dict[str, Any]:
    """A kernel-shaped gate finding (finding/criteria/evidence/location + Pass-3 severity) →
    a ``common.finding`` (severity/dimension/detail[/citations]) for ``review_result``."""
    criteria = [c for c in (f.get("criteria") or []) if isinstance(c, str)]
    detail = str(f.get("finding") or "")
    for extra in f.get("merged_from") or []:
        detail += f"\n(also: {extra})"
    for ev in f.get("evidence") or []:
        detail += f"\nevidence: {ev}"
    raw_sev = str(f.get("severity") or "").lower()
    out: dict[str, Any] = {
        "severity": _KERNEL_TO_COMMON_SEVERITY.get(raw_sev, _SEVERITY_DEFAULT),
        "dimension": (criteria[0] if criteria else "code-review"),
        "detail": detail.strip(),
    }
    path, line = _parse_location(f.get("location"))
    if path:
        cit: dict[str, Any] = {"kind": "file", "path": path}
        if line is not None:
            cit["line_start"] = line
        out["citations"] = [cit]
    if isinstance(f.get("reviewer_id"), str):
        out["reviewer_id"] = f["reviewer_id"]
    return out


def _verdict_to_review_result(
    verdict: dict[str, Any], *, base: str, head: str, changed_files: list[str]
) -> dict[str, Any]:
    from rebar.llm import findings as _findings

    blocking = list(verdict.get("blocking") or [])
    advisory = list(verdict.get("advisory") or [])
    common = [_to_common_finding(f) for f in (blocking + advisory)]
    reviewers = sorted(
        {r for f in (blocking + advisory) if isinstance(r := f.get("reviewer_id"), str)}
    )
    result = _findings.finalize_findings(
        common,
        runner=str(verdict.get("runner") or "code-review-gate"),
        model=verdict.get("model"),
        target={"kind": "code", "commits": [base, head], "files": changed_files},
        reviewers=reviewers,  # always a list (possibly empty) so `reviewers` is in the contract
        summary=f"{verdict.get('verdict')}: {len(blocking)} blocking, {len(advisory)} advisory.",
    )
    # Attach the typed gate verdict for callers that want it (coaching/coverage/the partition).
    result["verdict"] = verdict
    return result


def _disabled_review_result() -> dict[str, Any]:
    from rebar.llm import findings as _findings

    return _findings.finalize_findings(
        [],
        runner="code-review-disabled",
        model=None,
        target={"kind": "code"},
        reviewers=[],  # present (empty) so the review_result contract keys are stable
        summary="code-review capability disabled (verify.enable_code_review is off).",
    )


def review_code(
    *,
    base: str = "HEAD~1",
    head: str = "HEAD",
    diff_text: str | None = None,
    changed_files: list[str] | None = None,
    commit_message: str = "",
    reviewers: list[str] | None = None,
    ref: str | None = None,
    source: str | None = None,
    target_ticket: str | None = None,
    session_id: str | None = None,
    repo_root: Any = None,
    config: LLMConfig | None = None,
    runner: Any = None,
) -> dict[str, Any]:
    """Review a code change and return a ``review_result``. Gate-backed (epic b744): when the
    capability is disabled (default) returns an inert empty result; when enabled, runs the
    four-pass gate and translates its verdict. (``reviewers`` is accepted for surface
    compatibility but the gate selects its own overlays; ``ref``/``source`` are accepted but the
    v1 gate reviews the supplied diff/working tree — attested-snapshot scoping is a follow-on.)

    ``target_ticket`` anchors the durable ``code_review`` sidecar directly; ``session_id`` (story
    paradoxal-balsamic-bubblefish) instead keys a LOCAL session artifact so ``rebar review-code``
    gains cross-run memory — both are forwarded to the gate request."""
    from rebar.llm.workflow import gate_dispatch

    if not gate_dispatch.code_review_enabled(repo_root):
        return _disabled_review_result()

    cfg = config or LLMConfig.from_env(repo_root=repo_root)
    verdict = gate_dispatch.produce_code_review_verdict(
        gate_dispatch.CodeReviewRequest(
            cfg,
            base=base,
            head=head,
            diff_text=diff_text,
            changed_files=changed_files,
            commit_message=commit_message,
            runner=runner,
            target_ticket=target_ticket,
            session_id=session_id,
            repo_root=repo_root,
        )
    )
    from rebar.llm.code_review import assemble

    cf = changed_files
    if cf is None and diff_text is not None:
        cf = assemble.changed_from_diff(diff_text)
    return _verdict_to_review_result(verdict, base=base, head=head, changed_files=list(cf or []))
