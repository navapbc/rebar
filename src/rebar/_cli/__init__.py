"""The rebar argparse CLI — the ``rebar`` entrypoint.

An in-process Python CLI. Its structure:

* ``argparse`` owns top-level tokenization (subcommand + REMAINDER); per-command
  flag parsing stays in each command's own implementation, so argument-error
  messages come from the unchanged impls.
* Help / overview / unknown-subcommand output comes from the pinned package-data
  strings in :mod:`rebar._cli._help`.
* Read and leaf-write commands dispatch **in-process** to
  ``rebar._engine_support.reads.main`` / ``rebar._commands.main`` with the
  per-command auto-init policy (:mod:`rebar._cli._init`).
* ``reconcile`` routes to ``python -m rebar_reconciler``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from rebar._cli import _help
from rebar._cli._init import ensure_initialized

# Read arms that auto-init only; the read path owns its own throttled reconverge.
_READS_INIT_ONLY = frozenset(
    {"show", "list", "list-epics", "next-batch", "deps", "ready", "search", "session-logs"}
)
# Read-compute arm the dispatcher ran with NO _ensure_initialized (self-manages).
_READS_NO_INIT = frozenset({"validate"})
# Field-read arms the dispatcher ran with FULL _ensure_initialized (ticket-lib-api.sh).
_FIELD_READS = frozenset({"get-file-impact", "get-verify-commands"})
# Resolution/display arms the dispatcher ran with FULL _ensure_initialized.
_LOOKUPS = frozenset({"exists", "resolve", "format"})
# Graph-traversal arm the dispatcher ran with FULL _ensure_initialized.
_DESCENDANTS = frozenset({"list-descendants"})
# Per-ticket gate arms the dispatcher ran with NO _ensure_initialized (they read
# transitively via `ticket show`, so the gate CLI itself does no auto-init).
_GATES = frozenset({"clarity-check", "check-ac", "quality-check", "summary"})
# Signature arms (native, no bash counterpart): `sign` is a write, `verify-signature`
# a read; both need an initialized store + the environment signing key.
_SIGNING = frozenset({"sign", "verify-signature"})
# Write/lifecycle arms (E3): full auto-init + reconverge before the in-process write.
_LIFECYCLE = frozenset({"transition", "reopen", "claim"})
# Compaction arms (E3): full auto-init before the in-process SNAPSHOT write.
_COMPACT = frozenset({"compact", "compact-all"})
# Bridge arms (E5): full auto-init UNLESS a tracker override is injected (test
# tracker), matching the dispatcher's `bridge-status`/`purge-bridge` arms.
_BRIDGE = frozenset({"bridge-status", "bridge-fsck", "purge-bridge"})
# Import/export arms (P1.2): NDJSON interop projection. `export` is a read
# (init-only); `import` composes writes (full init).
_IO = frozenset({"export", "import"})
# Leaf-write arms: full auto-init + reconverge before the in-process write.
_WRITES_FULL = frozenset(
    {
        "create",
        "comment",
        "link",
        "unlink",
        "revert",
        "edit",
        "tag",
        "untag",
        "archive",
        "set-file-impact",
        "set-verify-commands",
        "session-log",
    }
)


def _reconcile(argv: list[str]) -> int:
    """``rebar reconcile`` → ``python -m rebar_reconciler`` (mirrors cli.py)."""
    from rebar import config
    from rebar._engine import engine_env

    root = str(config.repo_root())
    args = list(argv)
    if not any(a == "--repo-root" or a.startswith("--repo-root=") for a in args):
        args += ["--repo-root", root]
    if not any(a == "--mode" or a.startswith("--mode=") for a in args):
        args += ["--mode", "dry-run"]
    # Launch under THIS interpreter (sys.executable), not a bare ``python3``: the
    # reconciler imports ``rebar.*`` in-package (Tier E E5b), so it needs the
    # rebar-capable interpreter; engine_env keeps the engine dir on PYTHONPATH so
    # the top-level ``rebar_reconciler`` package still resolves.
    return subprocess.call([sys.executable, "-m", "rebar_reconciler", *args], env=engine_env(root))


def _review(argv: list[str]) -> int:
    """``rebar review`` → rebar.llm.review_ticket (native; not a dispatcher arm).

    Like ``reconcile``, this is intercepted in main() before the bash-golden help
    system, so it owns its own ``--help``. JSON output conforms to the
    ``review_result`` schema (OUTPUT_SCHEMAS['review'])."""
    import argparse
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
    import argparse
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
    import argparse
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
    import argparse
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


def _workflow(argv: list[str]) -> int:
    """``rebar workflow <new|validate|run|status|result>`` → the workflow toolchain.

    A native ``rebar.llm.workflow`` op intercepted in main() (like review/reconcile),
    so it owns its own ``--help``. ``new`` scaffolds; ``validate`` lints; ``run``
    executes (sync; ``--dry-run`` = offline FakeRunner, no tokens); ``status``/
    ``result`` read a run's state via replay. The ``show`` (render) arm is WS-I.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="rebar workflow",
        description="Author, validate, and run git-native workflows (.rebar/workflows/*.yaml).",
    )
    subparsers = parser.add_subparsers(dest="cmd")

    p_new = subparsers.add_parser("new", help="scaffold a new valid skeleton workflow")
    p_new.add_argument("name", help="workflow id (lowercase; the file stem)")
    p_new.add_argument(
        "--output-file",
        "-o",
        help="write here ('-' for stdout); default .rebar/workflows/<name>.yaml",
    )
    p_new.add_argument("--force", action="store_true", help="overwrite an existing file")

    p_val = subparsers.add_parser("validate", help="validate/lint a workflow file")
    p_val.add_argument("file", help="path to a .rebar/workflows/<name>.yaml file")
    p_val.add_argument(
        "--dry-run",
        action="store_true",
        help="static validation without tokens (use `run --dry-run` to execute)",
    )
    p_val.add_argument(
        "--no-expressions",
        action="store_true",
        help="treat any ${{ }} expression as an error (expressions=off kill-switch)",
    )
    p_val.add_argument("--output", "-o", choices=["text", "json"], default="text")

    p_run = subparsers.add_parser("run", help="execute a workflow (sync)")
    p_run.add_argument("file", help="a workflow file path or a .rebar/workflows/<name> name")
    p_run.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="a workflow input (repeatable)",
    )
    p_run.add_argument("--ticket", help="persist run-state to this ticket (durable + resumable)")
    p_run.add_argument("--run-id", help="reuse a run id (idempotent resume)")
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        help="execute agent steps with the offline FakeRunner (no tokens)",
    )
    p_run.add_argument("--output", "-o", choices=["text", "json"], default="text")

    p_show = subparsers.add_parser("show", help="render a workflow as a Mermaid graph")
    p_show.add_argument("file", help="a workflow file path or a .rebar/workflows/<name> name")

    p_edit = subparsers.add_parser(
        "edit", help="open a workflow in the ephemeral bpmn-js visual editor (edit-time)"
    )
    p_edit.add_argument("file", help="path to a .rebar/workflows/<name>.yaml file")
    p_edit.add_argument("--port", type=int, default=0, help="local port (default: ephemeral)")
    p_edit.add_argument("--no-open", action="store_true", help="do not auto-open the browser")

    for sub in ("status", "result"):
        p = subparsers.add_parser(sub, help=f"read a run's {sub} via replay")
        p.add_argument("run_id", help="the run id returned by `workflow run`")
        p.add_argument(
            "--ticket", help="the run's target ticket (else resolved from the run index)"
        )
        p.add_argument("--output", "-o", choices=["text", "json"], default="text")

    args = parser.parse_args(argv)
    if args.cmd == "new":
        return _workflow_new(args)
    if args.cmd == "validate":
        return _workflow_validate(args)
    if args.cmd == "run":
        return _workflow_run(args)
    if args.cmd == "show":
        return _workflow_show(args)
    if args.cmd == "edit":
        return _workflow_edit(args)
    if args.cmd in ("status", "result"):
        return _workflow_read(args)
    parser.print_help()
    return 1


def _workflow_edit(args) -> int:
    from rebar.llm import errors as _werr
    from rebar.llm.workflow import editor

    try:
        server, host, port, _token = editor.edit_workflow(
            args.file, port=args.port, open_browser=not args.no_open, serve_forever=False
        )
    except _werr.WorkflowError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    except OSError as exc:
        sys.stderr.write(f"Error: cannot start the editor server: {exc}\n")
        return 1
    sys.stderr.write(
        f"rebar visual editor for {args.file} at http://{host}:{port}/  (loopback only, "
        f"token-guarded; Save writes the IR file + a .bak). Press Ctrl-C to stop.\n"
    )
    try:
        import time

        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        sys.stderr.write("\neditor stopped.\n")
    finally:
        server.shutdown()
        server.server_close()
    return 0


def _workflow_show(args) -> int:
    from rebar.llm import errors as _werr
    from rebar.llm.workflow import render

    try:
        sys.stdout.write(render.render_workflow(args.file))
    except _werr.WorkflowError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    return 0


def _prompt(argv: list[str]) -> int:
    """``rebar prompt eval <id>`` → prompt evaluation (WS-G). Native intercept.

    Validates the git-tracked eval spec (offline; grader discipline + at_least(k) +
    coverage) and reports the DIRTY working-tree prompt's content hash (what would be
    evaluated). The live model run needs the ``eval`` extra + credentials (the eval
    CI); committing the prompt is required to apply a passing edit (git-canonical)."""
    import argparse

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
        rv = _prompts.get_reviewer(args.prompt_id)
        dirty_hash = _prompts.prompt_content_hash(
            _prompts.canonical_prompt_text(rv, repo_root=repo_root)
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
    import argparse

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


def _workflow_run(args) -> int:
    import json as _json

    import rebar
    from rebar.llm import errors as _werr

    inputs: dict[str, str] = {}
    for item in args.input:
        if "=" not in item:
            sys.stderr.write(f"Error: --input must be KEY=VALUE, got {item!r}\n")
            return 1
        key, _, val = item.partition("=")
        inputs[key] = val

    if args.ticket:
        ensure_initialized(init_only=False)  # run-state events are writes
    try:
        res = rebar.run_workflow(
            args.file,
            inputs,
            ticket_id=args.ticket,
            run_id=args.run_id,
            dry_run=args.dry_run,
        )
    except _werr.WorkflowError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    if args.output == "json":
        sys.stdout.write(_json.dumps(res) + "\n")
    else:
        sys.stdout.write(f"run_id: {res['run_id']}\n")
        sys.stdout.write(f"status: {res['status']}\n")
        if res.get("error"):
            sys.stdout.write(f"error: {res['error']}\n")
        for sid, st in res.get("steps", {}).items():
            sys.stdout.write(f"  - {sid}: {st}\n")
    return 0 if res["status"] == "succeeded" else 1


def _workflow_read(args) -> int:
    import json as _json

    import rebar
    from rebar.llm import errors as _werr

    if args.ticket:
        ensure_initialized(init_only=True)
    fn = rebar.get_workflow_status if args.cmd == "status" else rebar.get_workflow_result
    try:
        res = fn(args.run_id, args.ticket)
    except _werr.WorkflowError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    if args.output == "json":
        sys.stdout.write(_json.dumps(res) + "\n")
    else:
        sys.stdout.write(f"run_id: {res['run_id']}  ({res.get('status')})\n")
        if args.cmd == "status":
            for sid, st in res.get("steps", {}).items():
                sys.stdout.write(f"  - {sid}: {st}\n")
        else:
            sys.stdout.write(f"terminal_step: {res.get('terminal_step')}\n")
            sys.stdout.write(f"terminal_output: {_json.dumps(res.get('terminal_output'))}\n")
    return 0


def _workflow_new(args) -> int:
    from rebar import config
    from rebar.llm import errors as _werr
    from rebar.llm.workflow import lint as _lint
    from rebar.llm.workflow import templates as _templates

    try:
        content = _templates.scaffold(args.name)
    except _werr.WorkflowError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    # Self-check: the scaffold we hand out must itself be lint-clean.
    findings = _lint.lint_workflow(content, source=args.name)
    if not _lint.lint_passes(findings):  # pragma: no cover - guards a broken template
        sys.stderr.write("Error: internal scaffold is invalid:\n")
        for f in findings:
            sys.stderr.write(f"  {f}\n")
        return 1

    if args.output_file == "-":
        sys.stdout.write(content)
        return 0

    if args.output_file:
        dest = os.path.abspath(args.output_file)
    else:
        dest = os.path.join(str(config.repo_root()), ".rebar", "workflows", f"{args.name}.yaml")

    if os.path.exists(dest) and not args.force:
        sys.stderr.write(f"Error: {dest} already exists (use --force to overwrite)\n")
        return 1
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(content)
    sys.stdout.write(f"Created {dest}\n")
    return 0


def _workflow_validate(args) -> int:
    import json as _json

    from rebar import config
    from rebar.llm.workflow import lint as _lint

    try:
        with open(args.file, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        sys.stderr.write(f"Error: cannot read {args.file}: {exc}\n")
        return 1

    # check_prompts (WS-F2): validate agent `prompt:` refs resolve to a reviewer or
    # a .rebar/prompts/<id>.md file (repo-scoped).
    try:
        repo_root = str(config.repo_root())
    except Exception:  # not in a repo — skip the repo-scoped prompt-file lookup
        repo_root = None
    findings = _lint.lint_workflow(
        text,
        source=args.file,
        expressions=not args.no_expressions,
        check_prompts=True,
        repo_root=repo_root,
    )
    valid = _lint.lint_passes(findings)

    if args.output == "json":
        sys.stdout.write(
            _json.dumps(
                {
                    "source": args.file,
                    "valid": valid,
                    "dry_run": bool(args.dry_run),
                    "findings": [
                        {"location": f.location, "message": f.message, "severity": f.severity}
                        for f in findings
                    ],
                }
            )
            + "\n"
        )
        return 0 if valid else 1

    if args.dry_run:
        # The executor lands in WS-C; until then a dry run is the full static pass.
        # No LLM is ever called here, so "no tokens spent" holds by construction.
        sys.stdout.write(f"Dry run of {args.file} (static validation — no LLM calls):\n")
    if not findings:
        sys.stdout.write(f"OK: {args.file} is valid.\n")
        return 0
    for f in findings:
        sys.stdout.write(f"{f}\n")
    errs = sum(1 for f in findings if f.severity == "error")
    warns = len(findings) - errs
    sys.stdout.write(f"\n{errs} error(s), {warns} warning(s).\n")
    return 0 if valid else 1


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


def _bridge_probe(argv: list[str]) -> int:
    """``rebar bridge-probe`` → live Jira capability preflight.

    Launches the genuine python probe (``jira-capability-probe.py``) under
    ``sys.executable`` with ``engine_env`` (so the engine's
    ``rebar_reconciler.acli`` transport resolves) — replacing the bash-dispatcher
    passthrough (Tier E E6.5a). Talks only to Jira (creates + deletes a throwaway
    issue); needs no local tracker, so NO auto-init (matches the dispatcher arm).
    Output streams inherit so the operator sees the PROBE_PASS/FAIL lines directly.
    """
    from rebar._engine import engine_dir, engine_env

    script = str(engine_dir() / "jira-capability-probe.py")
    return subprocess.call([sys.executable, script, *argv], env=engine_env())


def _grounding_info(argv: list[str]) -> int:
    """``rebar grounding-info`` → the static code-grounding oracle contract.

    Repo-independent (no store, no auto-init). The ``report`` profile: a human
    summary by default, the ``grounding_info`` schema under ``--output json``.
    """
    import json as _json

    import rebar
    from rebar._engine_support.output import OutputFormatError, parse_output

    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    if rest:
        sys.stderr.write("Usage: rebar grounding-info [--output json]\n")
        return 1

    info = rebar.grounding_info()
    if fmt == "json":
        sys.stdout.write(_json.dumps(info, ensure_ascii=False) + "\n")
        return 0

    lines = [
        f"code-grounding oracle contract (dimensions v{info['dimensions_version']})",
        f"  dimensions:      {', '.join(info['dimensions'])}",
        f"  reference kinds: {', '.join(info['reference_kinds'])}",
        f"  abstain reasons: {', '.join(info['abstain_reasons'])}",
        f"  outcomes:        {', '.join(info['outcomes'])}",
        f"  jobs:            {', '.join(info['jobs'])}",
        f"  tiers:           {', '.join(info['provenance_tiers'])}",
        "  backends:",
    ]
    for b in info["backends"]:
        mark = "available" if b["available"] else "unavailable"
        ver = f" {b['version']}" if b.get("version") else ""
        lines.append(f"    - {b['name']}: {mark}{ver}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def _emit_subcommand_help(sub: str) -> int:
    """Print ``sub``'s usage (``_print_subcommand_help`` parity).

    Known subcommand → stdout, exit 0. Unknown → error + blank + overview all to
    stderr, exit 1 (the dispatcher's ``*)`` arm).
    """
    text = _help.subcommand_help(sub)
    if text is not None:
        sys.stdout.write(text)
        return 0
    sys.stderr.write(f"Error: unknown subcommand '{sub}'\n\n")
    sys.stderr.write(_help.overview())
    return 1


def _dispatch(sub: str, rest: list[str]) -> int:
    """Route a known subcommand to its in-process or passthrough implementation."""
    if sub == "init":
        # Explicit bootstrap — NEVER triggers auto-init (it IS init).
        from rebar._commands import init as _init_cmd

        return _init_cmd.init_cli(rest)
    if sub == "scratch":
        # Filesystem-only per-ticket store — NO auto-init (matches the dispatcher).
        from rebar._commands import scratch

        return scratch.scratch_cli(rest)
    if sub in _READS_INIT_ONLY:
        ensure_initialized(init_only=True)
        from rebar._engine_support import reads

        return reads.main([sub, *rest])
    if sub in _READS_NO_INIT:
        from rebar._engine_support import reads

        return reads.main([sub, *rest])
    if sub in _LIFECYCLE:
        ensure_initialized(init_only=False)
        from rebar._commands import transition as _transition

        if sub == "reopen":
            return _transition.reopen_cli(rest)
        if sub == "claim":
            return _transition.claim_cli(rest)
        return _transition.transition_cli(rest)
    if sub in _COMPACT:
        ensure_initialized(init_only=False)
        from rebar._commands import compact as _compact

        if sub == "compact-all":
            return _compact.compact_all_cli(rest)
        return _compact.compact_cli(rest)
    if sub == "delete":
        ensure_initialized(init_only=False)
        from rebar._commands import delete as _delete

        return _delete.delete_cli(rest)
    if sub in _BRIDGE:
        from rebar import config

        # The dispatcher auto-inits only when no test tracker is injected.
        if not config.tracker_dir_override():
            ensure_initialized(init_only=False)
        tracker = str(config.tracker_dir())
        if sub == "bridge-status":
            from rebar._engine_support import bridge

            return bridge.bridge_status_cli(rest, tracker)
        if sub == "bridge-fsck":
            from rebar._engine_support import bridge_fsck

            return bridge_fsck.main(rest)
        from rebar._commands import purge_bridge

        return purge_bridge.purge_bridge_cli(rest)
    if sub in _IO:
        from rebar._io import _cli as _io_cli

        if sub == "import":
            ensure_initialized(init_only=False)
            return _io_cli.import_cli(rest)
        ensure_initialized(init_only=True)
        return _io_cli.export_cli(rest)
    if sub == "fsck":
        ensure_initialized(init_only=False)
        from rebar._commands import fsck as _fsck

        return _fsck.fsck_cli(rest)
    if sub == "fsck-recover":
        # The recover path resolves its own tracker (honors REBAR_TRACKER_DIR /
        # TICKETS_TRACKER_DIR / --tracker-dir); the dispatcher only auto-inits when
        # no tracker is injected.
        from rebar import config

        if not config.tracker_dir_override():
            ensure_initialized(init_only=False)
        from rebar._commands import fsck_recover as _fsck_recover

        return _fsck_recover.fsck_recover_cli(rest)
    if sub in _WRITES_FULL:
        ensure_initialized(init_only=False)
        from rebar._commands import main as commands_main

        return commands_main([sub, *rest])
    if sub in _FIELD_READS:
        ensure_initialized(init_only=False)
        from rebar._engine_support import field_reads, reads

        tracker = reads.tracker_dir()
        if sub == "get-file-impact":
            return field_reads.file_impact_cli(rest, tracker)
        return field_reads.verify_commands_cli(rest, tracker)
    if sub in _LOOKUPS:
        ensure_initialized(init_only=False)
        from rebar._engine_support import lookups, reads

        tracker = reads.tracker_dir()
        if sub == "exists":
            return lookups.exists_cli(rest, tracker)
        if sub == "resolve":
            return lookups.resolve_cli(rest, tracker)
        return lookups.format_cli(rest, tracker, os.path.dirname(tracker))
    if sub in _DESCENDANTS:
        ensure_initialized(init_only=False)
        from rebar._engine_support import descendants, reads

        return descendants.list_descendants_cli(rest, reads.tracker_dir())
    if sub in _GATES:
        from rebar._engine_support import gates, reads

        tracker = reads.tracker_dir()
        if sub == "check-ac":
            return gates.check_ac_cli(rest, tracker)
        if sub == "clarity-check":
            return gates.clarity_check_cli(rest, tracker, os.path.dirname(tracker))
        if sub == "quality-check":
            return gates.quality_check_cli(rest, tracker)
        return gates.summary_cli(rest, tracker)
    if sub in _SIGNING:
        ensure_initialized(init_only=False)
        from rebar import signing

        if sub == "sign":
            return signing.sign_cli(rest)
        return signing.verify_signature_cli(rest)
    if sub == "bridge-probe":
        return _bridge_probe(rest)
    if sub == "grounding-info":
        # Repo-INDEPENDENT static read (no store, no auto-init): the code-grounding
        # oracle integration contract. Owns its own --output parsing (report profile).
        return _grounding_info(rest)
    # Every known subcommand is routed in-process above, and main() rejects
    # unknown subcommands before reaching _dispatch. Arriving here means a
    # subcommand was added to the known set without an in-process arm — a wiring
    # bug, surfaced loudly rather than silently mis-dispatched.
    raise RuntimeError(f"rebar: subcommand {sub!r} is known but has no in-process handler")


def main(argv: list[str] | None = None) -> int:
    """rebar CLI entry. Returns the process exit code.

    Control flow mirrors the bash dispatcher's help-interception-before-dispatch
    order so no command is executed on a help request and the streams/exit codes
    match the pinned goldens.
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    # Global config overrides (git -c style): `rebar -c section.key=value [...] <cmd>`,
    # repeatable, BEFORE the subcommand. They install the highest-precedence `cli`
    # layer (CLI > env > project > user > defaults) for every config consumer this
    # invocation — the verify gate, push/pull policy, display mode, etc.
    _overrides: list[str] = []
    while argv and (argv[0] in ("-c", "--config") or argv[0].startswith("--config=")):
        tok = argv.pop(0)
        if tok.startswith("--config="):
            _overrides.append(tok[len("--config=") :])
        elif argv:
            _overrides.append(argv.pop(0))
        else:
            sys.stderr.write(f"Error: {tok} requires a SECTION.KEY=VALUE argument\n")
            return 1
    if _overrides:
        from rebar import config as _config

        try:
            _config.set_cli_overrides(_config.parse_cli_overrides(_overrides))
        except _config.ConfigError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1

    # reconcile intercept (the dispatcher has no reconcile arm).
    if argv and argv[0] == "reconcile":
        return _reconcile(argv[1:])

    # review intercept (native rebar.llm op; not a dispatcher arm, like reconcile).
    if argv and argv[0] == "review":
        return _review(argv[1:])

    # review-code intercept (native rebar.llm code-review op).
    if argv and argv[0] == "review-code":
        return _review_code(argv[1:])

    # scan-spec intercept (native rebar.llm batch spec-scan op).
    if argv and argv[0] == "scan-spec":
        return _scan_spec(argv[1:])

    if argv and argv[0] == "verify-completion":
        return _verify_completion(argv[1:])

    # workflow intercept (native rebar.llm.workflow DSL toolchain; owns its --help).
    if argv and argv[0] == "workflow":
        return _workflow(argv[1:])

    # llm intercept (the LLM-framework setup wizard; owns its --help).
    if argv and argv[0] == "llm":
        return _llm(argv[1:])

    # prompt intercept (prompt evals — WS-G; owns its --help).
    if argv and argv[0] == "prompt":
        return _prompt(argv[1:])

    # config intercept (native config-transparency read; owns its own --help, like
    # reconcile/review). No store init: it reads working-tree config files only.
    if argv and argv[0] == "config":
        from rebar._commands import show_config

        return show_config.config_cli(argv[1:])

    # No subcommand: overview to stdout, exit 1 (the dispatcher's _usage).
    if not argv:
        sys.stdout.write(_help.overview())
        return 1

    first = argv[0]

    # Top-level help: `rebar help [<sub>]`, `rebar --help`, `rebar -h`.
    if first in ("help", "--help", "-h"):
        if len(argv) >= 2:
            return _emit_subcommand_help(argv[1])
        sys.stdout.write(_help.overview())
        return 0

    sub, rest = first, argv[1:]

    # `rebar <sub> --help|-h` as the FIRST arg after the subcommand → usage, no exec.
    if rest and rest[0] in ("--help", "-h"):
        return _emit_subcommand_help(sub)

    # Unknown subcommand: error to stderr + overview to stdout, exit 1.
    if sub not in _help.known_subcommands():
        sys.stderr.write(f"Error: unknown subcommand '{sub}'\n")
        sys.stdout.write(_help.overview())
        return 1

    return _dispatch(sub, rest)


# argparse scaffold — a real ArgumentParser owns top-level tokenization. Help and
# errors are intercepted in main() so the byte-exact dispatcher contract wins; the
# parser exists so the CLI is argparse-structured and gains its tokenization.
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rebar", add_help=False)
    parser.add_argument("subcommand", nargs="?")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    return parser


if __name__ == "__main__":
    sys.exit(main())
