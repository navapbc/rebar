"""LLM / agent-operation CLI command handlers — extracted from ``rebar._cli.__init__``
to keep the argv router lean (module-size policy). Covers the review family — ``rebar
review`` / ``review-code`` / ``scan-spec`` / ``verify-completion`` / ``review-plan`` /
``explain`` — plus their shared ``--ref``/``--source`` controls and text renderers. The
eval / config cluster (``prompt`` / ``criteria`` / ``llm setup``) lives in the sibling
:mod:`rebar._cli._llm_eval_commands` and is re-exported below (module-size split), so
``main()`` in ``rebar._cli`` imports every entrypoint it dispatches to from here.
"""

from __future__ import annotations

import sys

from rebar._cli._init import ensure_initialized

# The eval / config command cluster (``prompt`` / ``criteria`` / ``llm setup``) lives in
# a sibling module (module-size split) and is re-exported here so ``main()`` in
# ``rebar._cli`` and existing importers (``from rebar._cli._llm_commands import _criteria``)
# keep resolving unchanged.
from rebar._cli._llm_eval_commands import _criteria, _llm, _prompt  # noqa: F401
from rebar._cli._parser import guard_parse_errors
from rebar._cli._parsers.advanced import llm as _llm_parsers


def _gate_source_error() -> type[Exception]:
    """The snapshot/ref-resolution error class to catch at the CLI boundary so an
    unresolvable/absent ref, a missing-credential fetch, or an unreachable object DB at
    REBAR_ROOT surfaces as a clean, actionable ``Error:`` line (attested fails closed) rather
    than a traceback. (An invalid ``--source`` is rejected earlier by argparse's choices.)"""
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
    """Map a shape-A gate result to an exit code, honouring the systemic-degrade disposition
    (story authorial-hated-blackbear). A PASS is 0. Otherwise, a persisted retryable disposition
    (``coverage.retryable``, set from the classifier's ``LLMOutcome``) → exit 11
    ("transient — retry"); a non-retryable INDETERMINATE → ``indeterminate_code`` (the gate's
    existing INDETERMINATE exit, UNCHANGED); any other non-PASS → 1. The class-specific message
    is printed to stderr as a side effect so the driving agent sees what to do."""
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
        # A signable PASS whose attestation was ATTEMPTED but FAILED to persist is NOT a
        # silent success: the review's sole durable product — the signature the claim gate
        # consumes — was lost to a recoverable condition (e.g. a git index.lock), so a later
        # `claim` still fails the gate. The discrimination (stale plan vs unreadable relation
        # vs transient) lives BELOW this CLI in `rebar.llm.plan_review.resign` so the MCP
        # surface applies the SAME rule (ticket ammonic-amoral-nabarlek); here it maps to
        # exit 11 ("transient — retry") with the classifier's message on stderr.
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

    Reviews a git range (``--base``/``--head``) or a ``--diff-file`` with one or
    more reviewers; JSON output conforms to the ``review_result`` schema."""
    import json as _json

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
    # Local memory key (story paradoxal-balsamic-bubblefish): resolve the shared session id so the
    # gate can emit/reuse a `code-review: session:<id>` artifact across `rebar review-code` runs. A
    # bare/headless invocation (no session var, no SessionStart shim) returns None → mint a
    # per-invocation uuid4 (NOT persisted): local convergence is intentionally INERT there, chosen
    # for isolation (no local→Gerrit bleed, no cross-session contamination). Genuine per-session
    # convergence arrives wherever a session lifecycle exports one of the session-id env vars.
    import uuid

    from rebar._commands.session_id import resolve_session_id

    session_id = resolve_session_id() or uuid.uuid4().hex
    try:
        result = llm.review_code(
            base=args.base,
            head=args.head,
            diff_text=diff_text,
            reviewers=args.reviewers,
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
        sys.stdout.write(_json.dumps(result) + "\n")
    else:
        _render_review_text(result)
        _render_source_line(result)
    # review-code is the off-by-default fail-safe capability (WS4): when disabled it returns an
    # INERT empty result (no verdict, no findings, zero LLM calls) — that is a clean success, not
    # a degradation, so exit 0. Without this the generic _disposition_exit_code would treat the
    # verdict-less result as a non-PASS and exit 1, breaking automation that checks exit codes.
    if result.get("runner") == "code-review-disabled":
        return 0
    # PASS/advisory→0, retryable systemic degrade→11, INDETERMINATE→2 (story blackbear).
    return _disposition_exit_code(result, indeterminate_code=2)


@guard_parse_errors
def _scan_spec(argv: list[str]) -> int:
    """``rebar scan-spec`` → rebar.llm.scan_epics_for_spec (native op).

    Scans open epics against a spec for gaps/conflicts/overlaps; JSON output
    conforms to the ``review_result`` schema."""
    import json as _json

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
        sys.stdout.write(_json.dumps(result) + "\n")
    else:
        _render_review_text(result)
        _render_source_line(result)
    return 0


@guard_parse_errors
def _verify_completion(argv: list[str]) -> int:
    """``rebar verify-completion`` → rebar.llm.verify_completion (native; like reconcile).

    Intercepted in main() before the bash-golden help system, so it owns its own ``--help``
    and ships NO pinned help arm (which keeps it out of the live-driving ``--output`` coverage
    guard, exactly like review/review-code/scan-spec). JSON output conforms to the
    ``completion_verdict`` schema (OUTPUT_SCHEMAS['verify_completion']). Exit 0 on PASS,
    1 on FAIL or error (scriptable, like ``verify-signature``)."""
    import json as _json

    parser = _llm_parsers.build_verify_completion(prog="rebar verify-completion")
    args = parser.parse_args(argv)

    from rebar import llm

    if args.check:
        sys.stdout.write(_json.dumps(llm.available_backends(), indent=2) + "\n")
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
    if args.output == "json":
        sys.stdout.write(_json.dumps(result) + "\n")
    else:
        _render_verdict_text(result)
        _render_source_line(result)
    # A verifier FAULT ("no verdict obtainable", bug 2a6f) is retryable, not a completion
    # judgement — exit 11 like every other transient degrade, so a caller scripting this verb
    # can retry instead of treating it as "criteria unmet". Same disposition the close gate
    # gives it; without this the standalone verb flattened it into the generic exit 1.
    if result.get("verdict_obtainable") is False:
        return 11
    return 0 if result.get("verdict") == "PASS" else 1


@guard_parse_errors
def _explain(argv: list[str]) -> int:
    """``rebar explain <criterion-id|guide>`` → a plan-review criterion's authoring-guide section
    (WS10), OR an author-facing prose guide (``plan`` / ``review``). A pure registry/guide READ (no
    LLM); owns its --help like review-plan. Exit 0 on success, 1 on a clear error (unknown
    id/guide / malformed registry / missing guide file)."""
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
    """``rebar review-plan`` → rebar.llm.review_plan (native; like verify-completion).

    Runs the four-pass plan-review gate on a ticket's whole plan, emits the
    ``REVIEW_RESULT`` sidecar, and (on a non-blocking PASS) signs a plan-review
    attestation so a subsequent ``claim`` passes the gate (when enabled). Needs the
    'agents' extra + a model API key to run the LLM tiers; the DET floor runs
    without them. A ticket that is not yet claimable (status closed/idea/blocked, or
    open but blocked by an unclosed dependency) fast-fails to INDETERMINATE with no LLM
    unless ``--force`` is passed. Exit 0 on PASS, 1 on BLOCK, 2 on INDETERMINATE."""
    import json as _json

    parser = _llm_parsers.build_review_plan(prog="rebar review-plan")
    args = parser.parse_args(argv)

    from rebar import llm

    if args.check:
        sys.stdout.write(_json.dumps(llm.available_backends(), indent=2) + "\n")
        return 0
    if args.status:
        if not args.ticket_id:
            parser.error("ticket_id is required")
        ensure_initialized(init_only=True)
        status = llm.plan_review_status(args.ticket_id)
        if args.output == "json":
            sys.stdout.write(_json.dumps(status) + "\n")
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
        )
    except llm.LLMError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    except _gate_source_error() as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    if args.output == "json":
        sys.stdout.write(_json.dumps(result) + "\n")
    else:
        _render_plan_review_text(result)
        _render_source_line(result)
    # PASS→0, retryable systemic degrade→11, INDETERMINATE→2 (unchanged), BLOCK→1 (story blackbear).
    return _disposition_exit_code(result, indeterminate_code=2)


@guard_parse_errors
def _sign_review(argv: list[str]) -> int:
    """``rebar sign-review`` → rebar.llm.resign_plan_review (native; owns its --help).

    The CHEAP recovery path (ticket middle-actinium-thrush): (re)persist the plan-review
    attestation for an ALREADY-COMPUTED, still-valid PASS verdict from the latest
    ``REVIEW_RESULT`` sidecar — WITHOUT re-running the multi-pass LLM review. No LLM, no
    network, no 'agents' extra. REFUSES (exit 1) when there is no PASS sidecar, or the plan
    changed since the review (stale). Exit 0 on a successful re-sign."""
    import json as _json

    parser = _llm_parsers.build_sign_review(prog="rebar sign-review")
    args = parser.parse_args(argv)

    if not args.ticket_id:
        parser.error("ticket_id is required")
    ensure_initialized(init_only=True)
    from rebar import llm

    result = llm.resign_plan_review(args.ticket_id)
    if args.output == "json":
        sys.stdout.write(_json.dumps(result) + "\n")
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
    """Name the LLM step calls that failed but did not fail the run
    (eclectic-industrial-argali). Absent from a clean run's coverage, so this prints only when
    something actually degraded — which is the point: repeated silent degradation used to be
    visible only by scraping the logs."""
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


def _render_plan_review_text(result: dict) -> None:
    """Human-readable plan-review summary (verdict + blocking/advisory + coaching)."""
    v = result.get("verdict", "?")
    sys.stdout.write(f"PLAN REVIEW: {v} for {result.get('ticket_id')}\n")
    # Idempotence short-circuit (feature b3e5): a REUSED verdict ran no LLM — the existing
    # attestation was still current. Mark it distinctly so a fresh review is not confused with a
    # cache hit (the JSON already carries coverage.idempotent_skip / runner="reused").
    if (result.get("coverage", {}) or {}).get("idempotent_skip"):
        sys.stdout.write(
            "  reused: existing attestation is still current — no LLM re-run "
            "(pass --force to re-review)\n"
        )
    # BLOCK verdict-reuse (bug 7e77): the stored BLOCK verdict was rendered back because the
    # plan and the reviewed code are both unchanged since it was recorded — no LLM re-run.
    # The JSON carries coverage.verdict_reuse / runner="reused"; exit code stays the BLOCK 1.
    if (result.get("coverage", {}) or {}).get("verdict_reuse"):
        sys.stdout.write(
            "  reused: stored BLOCK verdict is still current (plan and code unchanged) — "
            "no LLM re-run (pass --force to re-review)\n"
        )
    _render_step_failures(result)
    counts = (result.get("coverage", {}) or {}).get("counts", {}) or {}
    overflow = counts.get("advisory_overflow", 0)
    sys.stdout.write(
        f"  blocking={counts.get('blocking', 0)} "
        f"advisory={counts.get('advisory_surfaced', 0)} "
        f"overflow={overflow} "
        f"dropped={counts.get('dropped', 0)} indeterminate={counts.get('indeterminate', 0)}\n"
    )
    # Surface each indeterminate finding's reason (+ remediation) so a non-PASS with no
    # blocking findings — e.g. a not-claimable fast-fail or a snapshot-collection error —
    # tells the reader WHY and how to proceed, not just a bare count.
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
        sys.stdout.write(
            f"  [advisory {','.join(f.get('criteria', []))} "
            f"sev={f.get('severity')}] {f.get('finding', '')}\n"
        )
    if overflow:
        # The surfaced advisory list is capped; tell the reader the tail exists (it is
        # NOT "only N issues") and where the full set lives, so a capped list never
        # reads as a complete count.
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
        sys.stdout.write(f"\n[{f.get('severity', '?').upper()}] ({f.get('dimension')}) ")
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
