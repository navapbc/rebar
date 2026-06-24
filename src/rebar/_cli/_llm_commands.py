"""LLM / agent-operation CLI command handlers — extracted from ``rebar._cli.__init__``
to keep the argv router lean (module-size policy). Covers ``rebar review`` /
``review-code`` / ``scan-spec`` / ``verify-completion`` / ``review-plan`` and the
``prompt`` + ``llm`` setup commands, plus their text renderers. ``main()`` in
``rebar._cli`` imports the entrypoints it dispatches to.
"""

from __future__ import annotations

import argparse
import os
import sys

from rebar._cli._init import ensure_initialized


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
    args = parser.parse_args(argv)

    from rebar import llm

    if args.check:
        sys.stdout.write(_json.dumps(llm.available_backends(), indent=2) + "\n")
        return 0
    if not args.ticket_id:
        parser.error("ticket_id is required")
    ensure_initialized(init_only=True)
    try:
        result = llm.review_ticket(args.ticket_id, args.reviewer_id, graph=args.graph)
    except llm.LLMError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    if args.output == "json":
        sys.stdout.write(_json.dumps(result) + "\n")
    else:
        _render_review_text(result)
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
    try:
        result = llm.review_code(
            base=args.base, head=args.head, diff_text=diff_text, reviewers=args.reviewers
        )
    except llm.LLMError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    if args.output == "json":
        sys.stdout.write(_json.dumps(result) + "\n")
    else:
        _render_review_text(result)
    return 0


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
        result = llm.scan_epics_for_spec(spec_text, epics=args.epics, batch_size=args.batch_size)
    except llm.LLMError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    if args.output == "json":
        sys.stdout.write(_json.dumps(result) + "\n")
    else:
        _render_review_text(result)
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
        action="store_true",
        help="include the ticket's descendants (default: auto — on for epics)",
    )
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    parser.add_argument(
        "--check", action="store_true", help="print backend/credential availability and exit"
    )
    args = parser.parse_args(argv)

    from rebar import llm

    if args.check:
        sys.stdout.write(_json.dumps(llm.available_backends(), indent=2) + "\n")
        return 0
    if not args.ticket_id:
        parser.error("ticket_id is required")
    ensure_initialized(init_only=True)
    try:
        result = llm.verify_completion(args.ticket_id, graph=True if args.graph else None)
    except llm.LLMError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    if args.output == "json":
        sys.stdout.write(_json.dumps(result) + "\n")
    else:
        _render_verdict_text(result)
    return 0 if result.get("verdict") == "PASS" else 1


def _review_plan(argv: list[str]) -> int:
    """``rebar review-plan`` → rebar.llm.review_plan (native; like verify-completion).

    Runs the three-pass plan-review gate on a ticket's whole plan, emits the
    ``REVIEW_RESULT`` sidecar, and (on a non-blocking PASS) signs a plan-review
    attestation so a subsequent ``claim`` passes the gate (when enabled). Needs the
    'agents' extra + a model API key to run the LLM tiers; the DET floor runs
    without them. Exit 0 on PASS, 1 on BLOCK, 2 on INDETERMINATE."""
    import json as _json

    parser = argparse.ArgumentParser(
        prog="rebar review-plan",
        description="Run the plan-review gate on a ticket: a deterministic Layer-1 floor + a "
        "three-pass (find → verify → decide) advisory coaching review of the plan, then sign a "
        "plan-review attestation on a non-blocking PASS. The inverse of verify-completion.",
    )
    parser.add_argument("ticket_id", nargs="?", help="ticket id, short id, or alias")
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    parser.add_argument(
        "--no-sign", action="store_true", help="run the review but do NOT sign an attestation"
    )
    parser.add_argument(
        "--check", action="store_true", help="print backend/credential availability and exit"
    )
    args = parser.parse_args(argv)

    from rebar import llm

    if args.check:
        sys.stdout.write(_json.dumps(llm.available_backends(), indent=2) + "\n")
        return 0
    if not args.ticket_id:
        parser.error("ticket_id is required")
    ensure_initialized(init_only=True)
    try:
        result = llm.review_plan(args.ticket_id, sign=not args.no_sign)
    except llm.LLMError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    if args.output == "json":
        sys.stdout.write(_json.dumps(result) + "\n")
    else:
        _render_plan_review_text(result)
    verdict = result.get("verdict")
    return 0 if verdict == "PASS" else (2 if verdict == "INDETERMINATE" else 1)


def _render_plan_review_text(result: dict) -> None:
    """Human-readable plan-review summary (verdict + blocking/advisory + coaching)."""
    v = result.get("verdict", "?")
    sys.stdout.write(f"PLAN REVIEW: {v} for {result.get('ticket_id')}\n")
    counts = (result.get("coverage", {}) or {}).get("counts", {}) or {}
    sys.stdout.write(
        f"  blocking={counts.get('blocking', 0)} "
        f"advisory={counts.get('advisory_surfaced', 0)} "
        f"dropped={counts.get('dropped', 0)} indeterminate={counts.get('indeterminate', 0)}\n"
    )
    for f in result.get("blocking", []):
        sys.stdout.write(f"  [BLOCK {','.join(f.get('criteria', []))}] {f.get('finding', '')}\n")
    for f in result.get("advisory", []):
        sys.stdout.write(
            f"  [advisory {','.join(f.get('criteria', []))} "
            f"sev={f.get('severity')}] {f.get('finding', '')}\n"
        )
    for c in result.get("coaching", []):
        sys.stdout.write(f"  → {c.get('coaching', '')}\n")
    sig = result.get("signature", {})
    if sig.get("signed"):
        sys.stdout.write("  signed: plan-review attestation written\n")


def _prompt(argv: list[str]) -> int:
    """``rebar prompt eval <id>`` → prompt evaluation (WS-G). Native intercept.

    Validates the git-tracked eval spec (offline; grader discipline + at_least(k) +
    coverage) and reports the DIRTY working-tree prompt's content hash (what would be
    evaluated). The live model run needs the ``eval`` extra + credentials (the eval
    CI); committing the prompt is required to apply a passing edit (git-canonical)."""

    parser = argparse.ArgumentParser(
        prog="rebar prompt", description="Evaluate git-canonical prompts."
    )
    subparsers = parser.add_subparsers(dest="cmd")
    p_eval = subparsers.add_parser("eval", help="validate + summarize a prompt's eval spec")
    p_eval.add_argument("prompt_id", help="prompt/reviewer id (e.g. code-quality)")
    p_eval.add_argument("--output", "-o", choices=["text", "json"], default="text")

    args = parser.parse_args(argv)
    if args.cmd == "eval":
        return _prompt_eval(args)
    parser.print_help()
    return 1


def _prompt_eval(args) -> int:
    import json as _json

    from rebar import config
    from rebar.llm import eval as _eval
    from rebar.llm import prompts as _prompts
    from rebar.llm.errors import LLMError

    try:
        repo_root = str(config.repo_root())
    except Exception:
        repo_root = None
    try:
        spec = _eval.load_eval_spec(args.prompt_id, repo_root=repo_root)
    except LLMError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    # The DIRTY working-tree prompt is what an eval would evaluate (WS-G1).
    dirty_hash = None
    try:
        dirty_hash = _prompts.prompt_content_hash(
            _prompts.get_prompt(args.prompt_id, repo_root=repo_root).text
        )
    except LLMError:
        pass  # a user-file prompt without a catalog reviewer — spec is still valid

    scorers = spec.get("scorers", [])
    report = {
        "prompt": spec.get("prompt"),
        "valid": True,
        "epochs": spec.get("epochs"),
        "gate": spec.get("gate"),
        "coverage_threshold": spec.get("coverage_threshold"),
        "gating_scorers": [s.get("name") for s in scorers if s.get("type") == "deterministic"],
        "report_scorers": [s.get("name") for s in scorers if s.get("type") == "llm-judge"],
        "gold_set_size": len(spec.get("gold_set", [])),
        "dirty_prompt_sha256": dirty_hash,
    }
    if args.output == "json":
        sys.stdout.write(_json.dumps(report) + "\n")
    else:
        sys.stdout.write(f"Eval spec for prompt {report['prompt']!r}: VALID\n")
        sys.stdout.write(
            f"  epochs={report['epochs']}  gate={report['gate']}  "
            f"coverage>={report['coverage_threshold']}\n"
        )
        sys.stdout.write(f"  gating (deterministic): {report['gating_scorers']}\n")
        sys.stdout.write(f"  reporting (llm-judge):  {report['report_scorers']}\n")
        sys.stdout.write(f"  gold-set samples: {report['gold_set_size']}\n")
        if dirty_hash:
            sys.stdout.write(f"  dirty prompt sha256: {dirty_hash[:16]}…\n")
        sys.stdout.write(
            "  → live run needs the `eval` extra + credentials (eval CI); commit the "
            "prompt to apply a passing edit.\n"
        )
    return 0


def _llm(argv: list[str]) -> int:
    """``rebar llm setup`` → the LLM-framework onboarding wizard (WS-J2).

    Native intercept (owns its own --help). Detects installed extras + API keys,
    validates the engine with an offline FakeRunner dry-run (no tokens), and prints
    the recommended ``[tool.rebar.llm]`` config (optionally written to a file)."""

    parser = argparse.ArgumentParser(
        prog="rebar llm",
        description="Configure and check the rebar LLM framework.",
    )
    subparsers = parser.add_subparsers(dest="cmd")
    p_setup = subparsers.add_parser(
        "setup", help="detect extras/keys, validate with a FakeRunner, print config"
    )
    p_setup.add_argument(
        "--write", metavar="FILE", help="write the recommended [tool.rebar.llm] block to FILE"
    )
    p_setup.add_argument(
        "--otlp-endpoint",
        metavar="URL",
        help="configure the [tracing] OTLP sink endpoint (write-only — OTel is never "
        "read back into a rebar decision); defaults to $OTEL_EXPORTER_OTLP_ENDPOINT",
    )
    p_setup.add_argument("--output", "-o", choices=["text", "json"], default="text")

    args = parser.parse_args(argv)
    if args.cmd == "setup":
        return _llm_setup(args)
    parser.print_help()
    return 1


def _llm_setup(args) -> int:
    import json as _json

    import rebar
    from rebar import _optional
    from rebar.llm import config as _cfg

    backends = _cfg.available_backends()
    extras = {e: _optional.extra_installed(e) for e in ("agents", "eval", "tracing")}

    # Validate the configuration with an offline FakeRunner dry-run (no tokens):
    # if a trivial agent workflow runs, the engine + config are wired correctly.
    dry_ok, dry_err = True, None
    try:
        res = rebar.run_workflow(
            {
                "schema_version": "1",
                "name": "setup_check",
                "steps": [{"id": "check", "prompt": "code_quality", "mode": "text"}],
            },
            dry_run=True,
        )
        dry_ok = res["status"] == "succeeded"
        dry_err = res.get("error")
    except Exception as exc:  # noqa: BLE001 - report any failure as a clean message
        dry_ok, dry_err = False, str(exc)

    # Optionally configure the OTLP tracing sink (write-only): an explicit
    # --otlp-endpoint, else the standard OTEL env var if set.
    otlp = args.otlp_endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    snippet = f'[tool.rebar.llm]\nmodel = "{_cfg.DEFAULT_MODEL}"\n'
    if otlp:
        snippet += (
            "\n# Write-only OTLP trace sink (OTel is never read back into a rebar "
            f'decision).\n[tool.rebar.llm.tracing]\notlp_endpoint = "{otlp}"\n'
        )
    report = {
        "extras": extras,
        "anthropic_api_key": backends["anthropic_api_key"],
        "openai_api_key": backends["openai_api_key"],
        "tracing_configured": backends.get("langfuse_configured", False),
        "otlp_endpoint": otlp,
        "dry_run_ok": dry_ok,
        "dry_run_error": dry_err,
        "recommended_config": snippet,
    }

    if args.output == "json":
        sys.stdout.write(_json.dumps(report) + "\n")
    else:
        sys.stdout.write("rebar LLM setup\n")
        sys.stdout.write(
            f"  extras:   agents={extras['agents']}  eval={extras['eval']}  "
            f"tracing={extras['tracing']}\n"
        )
        sys.stdout.write(
            f"  API keys: anthropic={backends['anthropic_api_key']}  "
            f"openai={backends['openai_api_key']}\n"
        )
        sys.stdout.write(
            f"  FakeRunner dry-run: {'OK' if dry_ok else 'FAILED: ' + (dry_err or '')}\n"
        )
        otlp_line = otlp or "not configured (--otlp-endpoint URL or $OTEL_EXPORTER_OTLP_ENDPOINT)"
        sys.stdout.write(f"  OTLP tracing sink: {otlp_line}\n")
        if not extras["agents"]:
            sys.stdout.write(
                "  → for real agent steps install:  pip install 'nava-rebar[agents]'\n"
            )
        if not (backends["anthropic_api_key"] or backends["openai_api_key"]):
            sys.stdout.write("  → set a provider API key (e.g. ANTHROPIC_API_KEY)\n")
        sys.stdout.write(f"\nRecommended config:\n\n{snippet}\n")

    if args.write:
        try:
            with open(args.write, "w", encoding="utf-8") as fh:
                fh.write(snippet)
            sys.stdout.write(f"Wrote {args.write}\n")
        except OSError as exc:
            sys.stderr.write(f"Error: cannot write {args.write}: {exc}\n")
            return 1
    return 0 if dry_ok else 1


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
