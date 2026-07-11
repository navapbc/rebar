"""LLM / agent-operation CLI command handlers — extracted from ``rebar._cli.__init__``
to keep the argv router lean (module-size policy). Covers the review family — ``rebar
review`` / ``review-code`` / ``scan-spec`` / ``verify-completion`` / ``review-plan`` /
``explain`` — plus their shared ``--ref``/``--source`` controls and text renderers. The
eval / config cluster (``prompt`` / ``criteria`` / ``llm setup``) lives in the sibling
:mod:`rebar._cli._llm_eval_commands` and is re-exported below (module-size split), so
``main()`` in ``rebar._cli`` imports every entrypoint it dispatches to from here.
"""

from __future__ import annotations

import argparse
import sys

from rebar._cli._init import ensure_initialized

# The eval / config command cluster (``prompt`` / ``criteria`` / ``llm setup``) lives in
# a sibling module (module-size split) and is re-exported here so ``main()`` in
# ``rebar._cli`` and existing importers (``from rebar._cli._llm_commands import _criteria``)
# keep resolving unchanged.
from rebar._cli._llm_eval_commands import _criteria, _llm, _prompt  # noqa: F401


def _add_ref_source(
    parser: argparse.ArgumentParser,
    *,
    ref_default: str = "origin/main",
    ref_configurable: bool = True,
) -> None:
    """Add the shared ``--ref`` / ``--source`` controls (epic raze-vet-ditch S5) to a
    code-reading CLI command, mirroring the MCP tools' ``ref``/``source`` args one-to-one.
    Both default to ``None`` so the configured default resolves (``REBAR_GATE_SOURCE`` /
    ``[snapshot]`` > built-in default). ``ref_configurable=False`` (review-code, whose ref
    defaults to the reviewed ``head``, not the cross-gate ``origin/main``) drops the
    config-override note so the help text matches the actual resolution."""
    ref_help = f"branch | tag | SHA to verify against (default: {ref_default}"
    ref_help += "; configurable via REBAR_GATE_REF / [snapshot].ref)" if ref_configurable else ")"
    if ref_configurable:
        ref_help += (
            " — pass --ref HEAD when the review depends on code you have committed "
            "locally but not yet landed on the default ref (a stacked change or feature "
            "branch): the default ref reads a snapshot predating that code, so symbols it "
            "adds read as 'does not exist' false findings"
        )
    parser.add_argument("--ref", default=None, help=ref_help)
    parser.add_argument(
        "--source",
        choices=["attested", "local"],
        default=None,
        help="attested (default): verify a snapshot pinned at --ref (signs, records "
        "verified_at_sha); local: read the in-place checkout (dirty allowed, never signs)",
    )


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

        msg = message_for(o.resolution_class.value)
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

        msg = message_for(rc)
        sys.stderr.write(f"llm-degrade: {rc} — {msg}\n" if msg else f"llm-degrade: {rc}\n")
    # `verdict` is a string on the plan-review result and the WHOLE nested gate verdict dict on
    # the code-review review_result (`shim._verdict_to_review_result` attaches it) — accept both.
    v = result.get("verdict")
    verdict = str((v.get("verdict", "") if isinstance(v, dict) else v) or "").upper()
    if verdict == "PASS":
        # A signable PASS whose attestation was ATTEMPTED but FAILED to persist (signed False
        # WITH an `error`, not a deliberate `reason` skip) is NOT a silent success: the
        # review's sole durable product — the signature the claim gate consumes — was lost to
        # a recoverable condition (e.g. a git index.lock), so a later `claim` still fails the
        # gate. Surface it as retryable (exit 11) so the expensive review is re-run, not
        # discarded. A deliberately-unsigned PASS (--no-sign / not-signable / drift → `reason`,
        # no `error`) and a successfully-signed PASS stay exit 0 (ticket middle-actinium-thrush).
        sig = result.get("signature") or {}
        if sig.get("signed") is False and sig.get("error"):
            sys.stderr.write(
                "plan review PASSED but the attestation could not be persisted: "
                f"{sig.get('error')}\n"
                "re-run `rebar review-plan` to retry — the claim gate needs the signature.\n"
            )
            return 11
        return 0
    if coverage.get("retryable"):
        return 11
    return indeterminate_code if verdict == "INDETERMINATE" else 1


def _review(argv: list[str]) -> int:
    """``rebar review`` → rebar.llm.review_ticket (native; not a dispatcher arm).

    Like ``reconcile``, this is intercepted in main() before the bash-golden help
    system, so it owns its own ``--help``. JSON output conforms to the
    ``review_result`` schema (OUTPUT_SCHEMAS['review'])."""
    import json as _json

    parser = argparse.ArgumentParser(
        prog="rebar review",
        description="Run an LLM review of a ticket (or its ticket-graph) and emit "
        "structured findings. Needs the 'agents' extra + a model API key (provider "
        "per REBAR_LLM_MODEL); see `rebar review --check`.",
    )
    parser.add_argument("ticket_id", nargs="?", help="ticket id, short id, or alias")
    parser.add_argument(
        "reviewer_id",
        nargs="?",
        default=None,
        help="reviewer from the catalog (default: the catalog's default reviewer)",
    )
    parser.add_argument(
        "--graph",
        action="store_true",
        help="also review the ticket's descendants, as one unit",
    )
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    parser.add_argument(
        "--check",
        action="store_true",
        help="print backend/credential availability and exit",
    )
    _add_ref_source(parser)
    args = parser.parse_args(argv)

    from rebar import llm

    if args.check:
        sys.stdout.write(_json.dumps(llm.available_backends(), indent=2) + "\n")
        return 0
    if not args.ticket_id:
        parser.error("ticket_id is required")
    ensure_initialized(init_only=True)
    try:
        result = llm.review_ticket(
            args.ticket_id, args.reviewer_id, graph=args.graph, ref=args.ref, source=args.source
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


def _review_code(argv: list[str]) -> int:
    """``rebar review-code`` → rebar.llm.review_code (native, like reconcile/review).

    Reviews a git range (``--base``/``--head``) or a ``--diff-file`` with one or
    more reviewers; JSON output conforms to the ``review_result`` schema."""
    import json as _json

    parser = argparse.ArgumentParser(
        prog="rebar review-code",
        description="Run an LLM code review of a change (git range or diff file) and "
        "emit aggregated structured findings. Needs the 'agents' extra + an API key.",
    )
    parser.add_argument("--base", default="HEAD~1", help="base git ref (default HEAD~1)")
    parser.add_argument("--head", default="HEAD", help="head git ref (default HEAD)")
    parser.add_argument("--diff-file", help="review this unified-diff file instead of a git range")
    parser.add_argument(
        "--reviewer",
        action="append",
        dest="reviewers",
        help="reviewer id (repeatable; default: deterministic selection)",
    )
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    _add_ref_source(parser, ref_default="the reviewed --head", ref_configurable=False)
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


def _scan_spec(argv: list[str]) -> int:
    """``rebar scan-spec`` → rebar.llm.scan_epics_for_spec (native op).

    Scans open epics against a spec for gaps/conflicts/overlaps; JSON output
    conforms to the ``review_result`` schema."""
    import json as _json

    parser = argparse.ArgumentParser(
        prog="rebar scan-spec",
        description="Batch-scan open epics against a specification and emit "
        "structured findings (gaps/conflicts/overlaps). Needs the 'agents' extra.",
    )
    parser.add_argument("--spec-file", required=True, help="path to the specification text")
    parser.add_argument("--batch-size", type=int, default=5, help="epics per batch (default 5)")
    parser.add_argument(
        "--epic",
        action="append",
        dest="epics",
        help="restrict to these epic ids (repeatable; default: all open epics)",
    )
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    _add_ref_source(parser)
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


def _verify_completion(argv: list[str]) -> int:
    """``rebar verify-completion`` → rebar.llm.verify_completion (native; like review).

    Intercepted in main() before the bash-golden help system, so it owns its own ``--help``
    and ships NO pinned help arm (which keeps it out of the live-driving ``--output`` coverage
    guard, exactly like review/review-code/scan-spec). JSON output conforms to the
    ``completion_verdict`` schema (OUTPUT_SCHEMAS['verify_completion']). Exit 0 on PASS,
    1 on FAIL or error (scriptable, like ``verify-signature``)."""
    import json as _json

    parser = argparse.ArgumentParser(
        prog="rebar verify-completion",
        description="Run the completion-verifier agent on a ticket and emit a PASS/FAIL verdict "
        "that its completion requirements (acceptance/success/close criteria, definitions of "
        "done; for bugs, that the bug is resolved) are demonstrably met by the implementation. "
        "Needs the 'agents' extra + a model API key; see `rebar verify-completion --check`.",
    )
    parser.add_argument("ticket_id", nargs="?", help="ticket id, short id, or alias")
    parser.add_argument(
        "--graph",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="include the ticket's descendants; use --no-graph to force own-criteria "
        "verification (default: auto — on for epics, off otherwise)",
    )
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    parser.add_argument(
        "--check", action="store_true", help="print backend/credential availability and exit"
    )
    _add_ref_source(parser)
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
    return 0 if result.get("verdict") == "PASS" else 1


def _explain(argv: list[str]) -> int:
    """``rebar explain <criterion-id>`` → the plan-review criteria authoring-guide section for a
    criterion (WS10). A pure registry/guide READ (no LLM); owns its --help like review-plan. Exit
    0 on success, 1 on a clear error (unknown id / malformed registry / missing guide file)."""
    import sys

    from rebar.llm.plan_review import registry

    parser = argparse.ArgumentParser(
        prog="rebar explain",
        description="Print the plan-review criteria authoring-guide section for a criterion id "
        "(e.g. `rebar explain F1`). One shared lookup with the MCP explain_criterion tool.",
    )
    parser.add_argument("criterion_id", nargs="?", help="a plan-review criterion id (e.g. F1, G3)")
    args = parser.parse_args(argv)
    if not args.criterion_id:
        parser.error("a criterion id is required (e.g. `rebar explain F1`)")
    try:
        sys.stdout.write(registry.explain_criterion(args.criterion_id) + "\n")
        return 0
    except registry.ExplainError as exc:
        sys.stderr.write(f"rebar explain: {exc} [{exc.kind}]\n")
        return 1


def _review_plan(argv: list[str]) -> int:
    """``rebar review-plan`` → rebar.llm.review_plan (native; like verify-completion).

    Runs the four-pass plan-review gate on a ticket's whole plan, emits the
    ``REVIEW_RESULT`` sidecar, and (on a non-blocking PASS) signs a plan-review
    attestation so a subsequent ``claim`` passes the gate (when enabled). Needs the
    'agents' extra + a model API key to run the LLM tiers; the DET floor runs
    without them. Exit 0 on PASS, 1 on BLOCK, 2 on INDETERMINATE."""
    import json as _json

    from rebar import config

    parser = argparse.ArgumentParser(
        prog="rebar review-plan",
        description="Run the plan-review gate on a ticket: a deterministic Layer-1 floor + a "
        "four-pass (find → verify → decide → coach) review of the plan, then sign a "
        "plan-review attestation on a non-blocking PASS. The inverse of verify-completion.",
        epilog=(
            "Coaching deep-links + `rebar explain <criterion-id>` reference the criteria "
            f"authoring guide at {config.plan_review_docs_url()} "
            "(anchor `#<criterion-id lower-cased>`; override the base with REBAR_DOCS_URL)."
        ),
    )
    parser.add_argument("ticket_id", nargs="?", help="ticket id, short id, or alias")
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    parser.add_argument(
        "--no-sign", action="store_true", help="run the review but do NOT sign an attestation"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-run the review even if a current attestation exists "
        "(bypass the idempotence short-circuit)",
    )
    parser.add_argument(
        "--check", action="store_true", help="print backend/credential availability and exit"
    )
    _add_ref_source(parser)
    args = parser.parse_args(argv)

    from rebar import llm

    if args.check:
        sys.stdout.write(_json.dumps(llm.available_backends(), indent=2) + "\n")
        return 0
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


def _sign_review(argv: list[str]) -> int:
    """``rebar sign-review`` → rebar.llm.resign_plan_review (native; owns its --help).

    The CHEAP recovery path (ticket middle-actinium-thrush): (re)persist the plan-review
    attestation for an ALREADY-COMPUTED, still-valid PASS verdict from the latest
    ``REVIEW_RESULT`` sidecar — WITHOUT re-running the multi-pass LLM review. No LLM, no
    network, no 'agents' extra. REFUSES (exit 1) when there is no PASS sidecar, or the plan
    changed since the review (stale). Exit 0 on a successful re-sign."""
    import json as _json

    parser = argparse.ArgumentParser(
        prog="rebar sign-review",
        description="Cheaply (re)persist the plan-review attestation for an already-computed, "
        "still-valid PASS verdict from the latest REVIEW_RESULT sidecar — WITHOUT re-running the "
        "multi-pass LLM review. Use it to recover a signature that a `rebar review-plan` computed "
        "but failed to persist (e.g. a transient git index.lock). Refuses to sign a non-PASS or a "
        "verdict that is stale because the plan changed since the review.",
    )
    parser.add_argument("ticket_id", nargs="?", help="ticket id, short id, or alias")
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
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


def _render_plan_review_text(result: dict) -> None:
    """Human-readable plan-review summary (verdict + blocking/advisory + coaching)."""
    v = result.get("verdict", "?")
    sys.stdout.write(f"PLAN REVIEW: {v} for {result.get('ticket_id')}\n")
    counts = (result.get("coverage", {}) or {}).get("counts", {}) or {}
    overflow = counts.get("advisory_overflow", 0)
    sys.stdout.write(
        f"  blocking={counts.get('blocking', 0)} "
        f"advisory={counts.get('advisory_surfaced', 0)} "
        f"overflow={overflow} "
        f"dropped={counts.get('dropped', 0)} indeterminate={counts.get('indeterminate', 0)}\n"
    )
    for f in result.get("blocking", []):
        sys.stdout.write(f"  [BLOCK {','.join(f.get('criteria', []))}] {f.get('finding', '')}\n")
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
