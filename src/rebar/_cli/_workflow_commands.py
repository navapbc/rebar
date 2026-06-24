"""Workflow-engine CLI command handlers — extracted from ``rebar._cli.__init__`` to
keep the argv router lean (module-size policy). ``_workflow`` is the subcommand
dispatcher over ``edit`` / ``show`` / ``run`` / ``read`` / ``new`` / ``validate``;
``main()`` in ``rebar._cli`` imports ``_workflow``.
"""

from __future__ import annotations

import argparse
import os
import sys

from rebar._cli._init import ensure_initialized


def _workflow(argv: list[str]) -> int:
    """``rebar workflow <new|validate|run|status|result>`` → the workflow toolchain.

    A native ``rebar.llm.workflow`` op intercepted in main() (like review/reconcile),
    so it owns its own ``--help``. ``new`` scaffolds; ``validate`` lints; ``run``
    executes (sync; ``--dry-run`` = offline FakeRunner, no tokens); ``status``/
    ``result`` read a run's state via replay. The ``show`` (render) arm is WS-I.
    """

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
