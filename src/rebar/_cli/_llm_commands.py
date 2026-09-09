"""Implement CLI handlers and text renderers for LLM-backed operations.

Review, scan, verification, plan, and explain commands reside here. Prompt, criteria,
and setup handlers are re-exported from the sibling evaluation module.
"""

from __future__ import annotations

import sys

from rebar._cli._init import ensure_initialized

# Re-export the split evaluation/config handlers so existing imports and main dispatch
# retain one stable surface.
from rebar._cli._llm_eval_commands import _criteria, _llm, _prompt  # noqa: F401
from rebar._cli._parser import guard_parse_errors
from rebar._cli._parsers.advanced import llm as _llm_parsers
from rebar._mcp_errors import js_safe_dumps


def _admission_refusal() -> tuple[type[Exception], ...]:
    """Return retryable gate-admission errors that need distinct CLI handling.

    These ``LLMError`` subclasses must precede generic arms that otherwise hardcode
    exit 1.
    """
    from rebar.llm.errors import GateCongestedError, GateScratchUnavailableError

    return (GateCongestedError, GateScratchUnavailableError)


def _gate_source_error() -> type[Exception]:
    """Return the snapshot error rendered cleanly for missing or unresolvable sources,
    credentials, or object databases."""
    from rebar._snapshot import SnapshotError

    return SnapshotError


def _render_source_line(result: dict) -> None:
    """Surface the source provenance (``source`` + ``verified_at_sha``) on a gate result."""
    src = result.get("source")
    if not src:
        return
    sha = result.get("verified_at_sha")
    tail = f" @ verified-at-sha {sha}" if sha else " (unsigned — in-place checkout)"
    sys.stdout.write(f"source: {src}{tail}\n")


def _render_record_line(record: dict) -> None:
    """Surface what a standalone ``verify-completion`` recorded on the ticket (story
    reuse-standalone-completion): whether the completion-verifier attestation was signed (so a
    later same-ref close reuses it) and whether the COMPLETION_VERDICT sidecar was written."""
    if not record:
        return
    if record.get("signed"):
        sys.stdout.write(
            "recorded: signed a completion-verifier attestation "
            "(a later same-ref close will reuse it)\n"
        )
        return
    cause = str(record.get("cause"))
    reasons = {
        "not_pass": "not signed (verdict is not PASS)",
        "sign_disabled": "not signed (--no-sign)",
        "local_source": "not signed (--source local is never certifiable)",
        "not_certifiable": "not signed (verdict is not certifiable)",
        "no_verified_sha": "not signed (no verified-at-sha to bind)",
        "sign_failed": f"attestation signing FAILED: {record.get('error', '')}",
    }
    detail = reasons.get(cause, f"not signed ({cause})")
    sidecar = "sidecar recorded" if record.get("sidecar_written") else "sidecar NOT recorded"
    sys.stdout.write(f"recorded: {detail}; {sidecar}\n")


def _llm_error_exit_code(exc: Exception) -> int:
    """Exit code for a RAISED ``LLMError`` (story blackbear): a retryable disposition attached by
    the classifier (`.outcome.retryable`) → exit 11 ("transient — retry"); else 1 (fail-closed).
    Used where a gate call raises rather than returning a degraded verdict dict."""
    from rebar.llm.failure import outcome_of

    o = outcome_of(exc)
    if o is not None and getattr(o, "retryable", False):
        from rebar.llm.failure import message_for

        msg = message_for(
            o.resolution_class.value, finish_reason=(o.diagnostic or {}).get("finish_reason")
        )
        if msg:
            sys.stderr.write(f"llm-degrade: {o.resolution_class.value} — {msg}\n")
        return 11
    return 1


def _disposition_exit_code(result: dict, *, indeterminate_code: int) -> int:
    """Map a gate result to its contractual exit code.

    PASS returns 0. Retryable degradation returns 11. Non-retryable INDETERMINATE
    uses ``indeterminate_code``. Other failures return 1. Resolution details go to
    stderr.
    """
    coverage = result.get("coverage") or {}
    rc = coverage.get("resolution_class")
    if rc:
        from rebar.llm.failure import message_for

        _fr = (coverage.get("diagnostic") or {}).get("finish_reason")
        msg = message_for(rc, finish_reason=_fr)
        sys.stderr.write(f"llm-degrade: {rc} — {msg}\n" if msg else f"llm-degrade: {rc}\n")
    # `verdict` is a string on the plan-review result and the WHOLE nested gate verdict dict on
    # the code-review review_result (`shim._verdict_to_review_result` attaches it) — accept both.
    v = result.get("verdict")
    verdict = str((v.get("verdict", "") if isinstance(v, dict) else v) or "").upper()
    if verdict == "PASS":
        # A PASS whose required signature could not persist is retryable, never successful.
        # Shared attestation classification keeps CLI and MCP behavior aligned.
        from rebar.llm.plan_review.resign import classify_plan_review_attestation

        attestation = classify_plan_review_attestation(result)
        if attestation.retryable:
            sys.stderr.write(attestation.message)
            return 11
        return 0
    if coverage.get("retryable"):
        return 11
    return indeterminate_code if verdict == "INDETERMINATE" else 1


@guard_parse_errors
def _review_code(argv: list[str]) -> int:
    """``rebar review-code`` → rebar.llm.review_code (native, like reconcile).

    Reviews a git range (``--base``/``--head``) or a ``--diff-file``; JSON output
    conforms to the ``review_result`` schema."""

    parser = _llm_parsers.build_review_code(prog="rebar review-code")
    args = parser.parse_args(argv)

    from rebar import llm

    diff_text = None
    if args.diff_file:
        try:
            with open(args.diff_file, encoding="utf-8", errors="replace") as fh:
                diff_text = fh.read()
        except OSError as exc:
            sys.stderr.write(f"Error: cannot read --diff-file: {exc}\n")
            return 1
    # Reuse a session-scoped review artifact when an exported session id exists. Headless calls
    # get an unpersisted UUID, preventing local/Gerrit or cross-session leakage.
    import uuid

    from rebar._commands.session_id import resolve_session_id

    session_id = resolve_session_id() or uuid.uuid4().hex
    try:
        result = llm.review_code(
            base=args.base,
            head=args.head,
            diff_text=diff_text,
            ref=args.ref,
            source=args.source,
            session_id=session_id,
        )
    except llm.LLMError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return _llm_error_exit_code(exc)
    except _gate_source_error() as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    if args.output == "json":
        sys.stdout.write(js_safe_dumps(result) + "\n")
    else:
        _render_review_text(result)
        _render_source_line(result)
    # PASS/advisory→0, retryable systemic degrade→11, INDETERMINATE→2 (story blackbear).
    return _disposition_exit_code(result, indeterminate_code=2)


@guard_parse_errors
def _scan_spec(argv: list[str]) -> int:
    """``rebar scan-spec`` → rebar.llm.scan_epics_for_spec (native op).

    Scans open epics against a spec for gaps/conflicts/overlaps; JSON output
    conforms to the ``review_result`` schema."""

    parser = _llm_parsers.build_scan_spec(prog="rebar scan-spec")
    args = parser.parse_args(argv)

    try:
        with open(args.spec_file, encoding="utf-8", errors="replace") as fh:
            spec_text = fh.read()
    except OSError as exc:
        sys.stderr.write(f"Error: cannot read --spec-file: {exc}\n")
        return 1
    ensure_initialized(init_only=True)  # reads epics from the store
    from rebar import llm

    try:
        result = llm.scan_epics_for_spec(
            spec_text,
            epics=args.epics,
            batch_size=args.batch_size,
            ref=args.ref,
            source=args.source,
        )
    except llm.LLMError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    except _gate_source_error() as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    if args.output == "json":
        sys.stdout.write(js_safe_dumps(result) + "\n")
    else:
        _render_review_text(result)
        _render_source_line(result)
    return 0


@guard_parse_errors
def _verify_completion(argv: list[str]) -> int:
    """Invoke ``rebar.llm.verify_completion`` for ``rebar verify-completion``.

    Top-level help is served from the committed parser artifact before handler dispatch.
    JSON output conforms to the ``completion_verdict`` schema in
    ``OUTPUT_SCHEMAS['verify_completion']``. The command returns zero on PASS and one on FAIL
    or error."""

    parser = _llm_parsers.build_verify_completion(prog="rebar verify-completion")
    args = parser.parse_args(argv)

    from rebar import llm

    if args.check:
        sys.stdout.write(js_safe_dumps(llm.available_backends(), indent=2) + "\n")
        return 0
    if not args.ticket_id:
        parser.error("ticket_id is required")
    ensure_initialized(init_only=True)
    try:
        result = llm.verify_completion(
            args.ticket_id, graph=args.graph, ref=args.ref, source=args.source
        )
    except llm.LLMError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        # Shape B (story blackbear): a retryable outage → exit 11 ("transient — retry"), else 1.
        return _llm_error_exit_code(exc)
    except _gate_source_error() as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    # Persist standalone completion evidence. A certifiable PASS signs evidence that a same-ref
    # close can reuse. PASS and FAIL write sidecars, while local or ``--no-sign`` runs write only
    # sidecars.
    from rebar._commands.transition_close import record_completion_verdict

    result["record"] = record_completion_verdict(result, args.ticket_id, sign=not args.no_sign)
    if args.output == "json":
        sys.stdout.write(js_safe_dumps(result) + "\n")
    else:
        _render_verdict_text(result)
        _render_source_line(result)
        _render_record_line(result["record"])
    # Verifier faults are retryable (exit 11), distinct from unmet criteria. The shared
    # completion classifier preserves that distinction for standalone and close paths.
    if result.get("verdict") == "PASS":
        return 0
    from rebar.llm import completion_reconcile

    return completion_reconcile.completion_fail_returncode(result)


@guard_parse_errors
def _explain(argv: list[str]) -> int:
    """Read a plan-review criterion section or a packaged author guide.

    This command does not call an LLM. Top-level help is served from the committed parser
    artifact. It returns zero on success and one for an unknown topic or invalid source."""
    import sys

    from rebar.llm.plan_review import registry

    guides = ", ".join(sorted(registry.AUTHOR_GUIDES))
    parser = _llm_parsers.build_explain(prog="rebar explain")
    args = parser.parse_args(argv)
    if not args.topic:
        parser.error(f"a criterion id (e.g. F1) or a guide ({guides}) is required")
    try:
        if args.topic in registry.AUTHOR_GUIDES:
            sys.stdout.write(registry.explain_guide(args.topic))
        else:
            sys.stdout.write(registry.explain_criterion(args.topic) + "\n")
        return 0
    except registry.ExplainError as exc:
        sys.stderr.write(f"rebar explain: {exc} [{exc.kind}]\n")
        return 1


@guard_parse_errors
def _review_plan(argv: list[str]) -> int:
    """Run, record, and optionally sign the multi-pass plan review.

    A non-blocking PASS signs the attestation consumed by claim. Ineligible ticket
    states fail fast without LLM work unless human ``--force`` is supplied. The
    command exits 0 for PASS, 1 for BLOCK, and 2 for INDETERMINATE. LLM tiers require
    the agents extra and model credentials.
    """

    parser = _llm_parsers.build_review_plan(prog="rebar review-plan")
    args = parser.parse_args(argv)

    # ``--retry`` accepts only the latest eligible INDETERMINATE and conflicts with force,
    # status, or check. ``--no-sign`` remains compatible.
    if getattr(args, "retry", False):
        conflict = next((f for f in ("force", "status", "check") if getattr(args, f, False)), None)
        if conflict is not None:
            parser.error(f"--retry cannot be combined with --{conflict}")

    from rebar import llm

    if args.check:
        sys.stdout.write(js_safe_dumps(llm.available_backends(), indent=2) + "\n")
        return 0
    if args.status:
        if not args.ticket_id:
            parser.error("ticket_id is required")
        ensure_initialized(init_only=True)
        status = llm.plan_review_status(args.ticket_id)
        if args.output == "json":
            sys.stdout.write(js_safe_dumps(status) + "\n")
        else:
            sha = status.get("verified_at_sha") or "unknown"
            sys.stdout.write(f"PLAN REVIEW STATUS: {status['verdict']} for {args.ticket_id}\n")
            basis = status.get("currency_basis") or "unknown"
            sys.stdout.write(
                f"  current={status['ok']} verified-at-sha={sha} currency-basis={basis}\n"
            )
            sys.stdout.write(f"  {status['reason']}\n")
        # Distinct from a review's PASS/BLOCK/INDETERMINATE codes: 0 current, 12 not current.
        return 0 if status["ok"] else 12
    if not args.ticket_id:
        parser.error("ticket_id is required")
    ensure_initialized(init_only=True)
    try:
        result = llm.review_plan(
            args.ticket_id,
            ref=args.ref,
            source=args.source,
            sign=not args.no_sign,
            force=args.force,
            retry=getattr(args, "retry", False),
        )
    except _admission_refusal() as exc:
        # Admission congestion means the gate never ran, so return retryable exit 11 rather
        # than BLOCK or INDETERMINATE.
        sys.stderr.write(f"Error: {exc}\n")
        return _llm_error_exit_code(exc)
    except llm.LLMError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    except _gate_source_error() as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    # An ineligible --retry REFUSES before any model call: print the full-review remedy to stderr
    # (the review itself carries the machine-readable reason on coverage.retry_refused) and exit 2.
    if getattr(args, "retry", False) and (result.get("coverage") or {}).get("retry_refused"):
        from rebar.llm.plan_review.retry import REMEDY

        sys.stderr.write(REMEDY + "\n")
    if args.output == "json":
        sys.stdout.write(js_safe_dumps(result) + "\n")
    else:
        _render_plan_review_text(result)
        _render_source_line(result)
    # PASS→0, retryable systemic degrade→11, INDETERMINATE→2 (unchanged), BLOCK→1 (story blackbear).
    return _disposition_exit_code(result, indeterminate_code=2)


@guard_parse_errors
def _sign_review(argv: list[str]) -> int:
    """Re-sign the latest still-current PASS without rerunning review.

    This LLM- and network-free recovery path refuses absent, non-PASS, or stale
    sidecars. It exits 0 only after persisting the attestation.
    """

    parser = _llm_parsers.build_sign_review(prog="rebar sign-review")
    args = parser.parse_args(argv)

    if not args.ticket_id:
        parser.error("ticket_id is required")
    ensure_initialized(init_only=True)
    from rebar import llm

    result = llm.resign_plan_review(args.ticket_id)
    if args.output == "json":
        sys.stdout.write(js_safe_dumps(result) + "\n")
    else:
        if result.get("ok"):
            sys.stdout.write(
                f"SIGN REVIEW: signed plan-review attestation for {result.get('ticket_id')}\n"
                f"  {result.get('reason', '')}\n"
            )
        else:
            sys.stderr.write(
                f"SIGN REVIEW: refused for {result.get('ticket_id')} — {result.get('reason', '')}\n"
            )
    # Exit 0 on a successful re-sign; non-zero on any refusal (absent / non-PASS / stale).
    return 0 if result.get("ok") else 1


def _render_step_failures(result: dict) -> None:
    """Render non-fatal LLM step failures recorded in coverage.

    Clean runs omit the line. Degraded runs name failed steps that contributed
    nothing.
    """
    tally = (result.get("coverage", {}) or {}).get("llm_step_failures") or {}
    if not tally:
        return
    by_step = tally.get("by_step", {}) or {}
    detail = ", ".join(f"{label}={n}" for label, n in sorted(by_step.items()))
    sys.stdout.write(
        f"  llm step failures: {tally.get('total', 0)}"
        f"{f' ({detail})' if detail else ''} — non-fatal, the verdict is unaffected; "
        "these steps contributed nothing to it\n"
    )


def _render_reuse_notation(result: dict) -> None:
    """Explain that unchanged findings were replayed, not freshly reviewed.

    Both reuse paths retain the force hint and, when available, show the stored
    review's timestamp and code SHA. JSON conveys the same state through coverage
    fields and the ``reused`` runner.
    """
    coverage = result.get("coverage", {}) or {}
    if coverage.get("idempotent_skip"):
        sys.stdout.write(
            "  reused: replaying the last review's result — the plan is unchanged and its "
            "attestation is still current, so no fresh LLM review ran "
            "(pass --force to re-review)\n"
        )
    elif coverage.get("verdict_reuse"):
        sys.stdout.write(
            "  reused: replaying the last review's stored BLOCK — the plan and the "
            "reviewed code are unchanged since it was recorded, so no fresh LLM review "
            "ran (pass --force to re-review)\n"
        )
    else:
        return
    anchor = coverage.get("replayed_review") or {}
    parts = []
    if anchor.get("reviewed_at"):
        from datetime import datetime, timezone

        stamp = datetime.fromtimestamp(anchor["reviewed_at"] / 1e9, tz=timezone.utc)
        parts.append(f"reviewed at {stamp.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    if anchor.get("verified_at_sha"):
        parts.append(f"against code {str(anchor['verified_at_sha'])[:12]}")
    if parts:
        sys.stdout.write(f"  last review: {', '.join(parts)}\n")


def _render_plan_review_text(result: dict) -> None:
    """Human-readable plan-review summary (verdict + blocking/advisory + coaching)."""
    v = result.get("verdict", "?")
    sys.stdout.write(f"PLAN REVIEW: {v} for {result.get('ticket_id')}\n")
    _render_reuse_notation(result)
    _render_step_failures(result)
    counts = (result.get("coverage", {}) or {}).get("counts", {}) or {}
    overflow = counts.get("advisory_overflow", 0)
    sys.stdout.write(
        f"  blocking={counts.get('blocking', 0)} "
        f"advisory={counts.get('advisory_surfaced', 0)} "
        f"overflow={overflow} "
        f"dropped={counts.get('dropped', 0)} indeterminate={counts.get('indeterminate', 0)}\n"
    )
    # Explain each indeterminate result and remedy, including fast-fail admission and
    # snapshot errors that have no blocking finding.
    for f in result.get("indeterminate", []):
        reason = f.get("reason") or f.get("finding") or ""
        sys.stdout.write(f"  [indeterminate {f.get('id', '')}] {reason}\n")
        if f.get("remediation"):
            sys.stdout.write(f"    → {f['remediation']}\n")
    blocking = result.get("blocking", [])
    group_sizes: dict = {}
    for f in blocking:
        if f.get("group_id"):
            group_sizes[f["group_id"]] = group_sizes.get(f["group_id"], 0) + 1
    for f in blocking:
        # Fix-unit grouping (story 5e64): render only each group's primary — the folded
        # members are the same defect co-cited by other criteria, summarized by the suffix.
        if f.get("group_id") and not f.get("is_primary"):
            continue
        suffix = ""
        folded = group_sizes.get(f.get("group_id"), 1) - 1
        if folded and f.get("group_criteria"):
            others = [c for c in f["group_criteria"] if c not in (f.get("criteria") or [])]
            suffix = f"  (+{folded} co-criteria: {', '.join(others)})"
        sys.stdout.write(
            f"  [BLOCK {','.join(f.get('criteria', []))}] {f.get('finding', '')}{suffix}\n"
        )
    for f in result.get("advisory", []):
        sys.stdout.write(f"  [advisory {','.join(f.get('criteria', []))}] {f.get('finding', '')}\n")
    if overflow:
        # Disclose capped advisory overflow and point to the complete sidecar.
        sys.stdout.write(
            f"  (+{overflow} more advisory finding(s) beyond the surfacing cap — "
            f"see the REVIEW_RESULT sidecar)\n"
        )
    for c in result.get("coaching", []):
        link = c.get("guide_url")
        sys.stdout.write(f"  → {c.get('coaching', '')}" + (f"  [{link}]\n" if link else "\n"))
    # Store-wide overlap advisories (epic only-crave-art) — a separate, advisory-only block
    # with ready-to-run link suggestions; NEVER part of the blocking/advisory verdict.
    overlap = result.get("overlap", [])
    if overlap:
        sys.stdout.write(
            f"  overlap: {len(overlap)} candidate cross-ticket relation(s) "
            f"(advisory — human confirmation, never auto-applied):\n"
        )
        for o in overlap:
            artifact = o.get("shared_artifact")
            sys.stdout.write(
                f"    ~ {o.get('relation')} (conf={o.get('confidence')}"
                + (f", shared: {artifact}" if artifact else "")
                + f"): {o.get('link_command', '')}\n"
            )
    sig = result.get("signature", {})
    if sig.get("signed"):
        sys.stdout.write("  signed: plan-review attestation written\n")


def _render_review_text(result: dict) -> None:
    """Human-readable rendering of a review_result."""
    findings = result.get("findings", [])
    target = result.get("target", {})
    ids = ", ".join(target.get("ticket_ids", [])) or "?"
    sys.stdout.write(
        f"Review of {ids} ({result.get('runner')}/{result.get('model') or 'n/a'}) — "
        f"{len(findings)} finding(s)\n"
    )
    if result.get("summary"):
        sys.stdout.write(f"\n{result['summary']}\n")
    for f in findings:
        if "decision" in f:
            tag = "BLOCKING" if f.get("decision") == "block" else "ADVISORY"
        else:
            tag = f.get("severity", "?").upper()
        sys.stdout.write(f"\n[{tag}] ({f.get('dimension')}) ")
        # Surface multi-reviewer consensus that aggregation computed (agreement>1).
        if f.get("agreement", 1) > 1:
            who = ", ".join(f.get("reviewers", [])) or "?"
            sys.stdout.write(f"[agreement {f['agreement']}: {who}] ")
        if f.get("title"):
            sys.stdout.write(f"{f['title']}\n")
        else:
            sys.stdout.write("\n")
        sys.stdout.write(f"  {f.get('detail', '')}\n")
        for c in f.get("citations", []):
            if c.get("kind") == "file":
                loc = c.get("path", "")
                if c.get("line_start"):
                    loc += f":{c['line_start']}"
                    if c.get("line_end") and c["line_end"] != c["line_start"]:
                        loc += f"-{c['line_end']}"
                sys.stdout.write(f"    @ {loc}\n")
            elif c.get("kind") == "url":
                sys.stdout.write(f"    @ {c.get('url', '')}\n")
            else:
                sys.stdout.write(f"    - {c.get('description', '')}\n")


def _render_verdict_text(result: dict) -> None:
    """Human-readable rendering of a completion_verdict (verdict + per-criterion findings)."""
    target = result.get("target", {})
    ids = ", ".join(target.get("ticket_ids", [])) or "?"
    findings = result.get("findings", [])
    sys.stdout.write(
        f"Completion verdict for {ids} "
        f"({result.get('runner')}/{result.get('model') or 'n/a'}): {result.get('verdict', '?')}\n"
    )
    if result.get("summary"):
        sys.stdout.write(f"\n{result['summary']}\n")
    if findings:
        noun = "criterion" if len(findings) == 1 else "criteria"
        # An insufficient-evidence FAIL (framework-derived top-level marker) is an evidence
        # GAP, not a refutation — say so instead of reporting the criteria as unmet.
        if result.get("evidence_sufficient") is False:
            sys.stdout.write(f"\nevidence insufficient for {len(findings)} {noun}:\n")
        else:
            sys.stdout.write(f"\n{len(findings)} unmet {noun}:\n")
    for f in findings:
        crit = f.get("criterion") or f.get("dimension") or "?"
        sys.stdout.write(f"\n[{f.get('severity', '?').upper()}] {crit}\n")
        sys.stdout.write(f"  {f.get('detail', '')}\n")
        for c in f.get("citations", []):
            if c.get("kind") == "file":
                loc = c.get("path", "")
                if c.get("line_start"):
                    loc += f":{c['line_start']}"
                    if c.get("line_end") and c["line_end"] != c["line_start"]:
                        loc += f"-{c['line_end']}"
                sys.stdout.write(f"    @ {loc}\n")
            elif c.get("kind") == "url":
                sys.stdout.write(f"    @ {c.get('url', '')}\n")
            else:
                sys.stdout.write(f"    - {c.get('description', '')}\n")
    # Remediation guidance rides on FAIL verdicts (reconcile_verdict): point the reader at the
    # evidence channel — documenting proof of a met requirement as a comment on the ticket.
    if result.get("remediation"):
        sys.stdout.write(f"\n{result['remediation']}\n")
