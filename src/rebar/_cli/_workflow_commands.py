"""Workflow-engine CLI command handlers — extracted from ``rebar._cli.__init__`` to
keep the argv router lean (module-size policy). ``_workflow`` is the subcommand
dispatcher over ``edit`` / ``show`` / ``run`` / ``read`` / ``new`` / ``validate``;
``main()`` in ``rebar._cli`` imports ``_workflow``.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from rebar._cli._init import ensure_initialized
from rebar._cli._parser import ParseError, render_parse_error


def _build_parser() -> argparse.ArgumentParser:
    """Build the ``rebar workflow`` parser via the lean S2c factory."""
    from rebar._cli._parsers.advanced import workflow as _workflow_parser

    return _workflow_parser.build(prog="rebar workflow")


# --- `rebar workflow new` scaffold (inline; a CLI concern, not a library export) ---
#
# Ships ONE schema-valid 3-step skeleton (scripted fetch -> agent review -> scripted
# gate) that ``rebar workflow new`` writes. It opens with a ``$schema`` modeline so
# editors with the YAML language server give inline completion/validation. The literal
# carries a ``__NAME__`` token (not a ``str.format`` field) so the ``${{ … }}``
# expressions in the body are left untouched. It is parsed + linted by
# tests/unit/workflow/test_cli.py so it can never drift invalid.
_SCAFFOLD_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

_SCAFFOLD_V1 = """\
# yaml-language-server: $schema=https://github.com/navapbc/rebar/schemas/workflow.v1.schema.json
schema_version: "1"
name: __NAME__
description: TODO — describe what this workflow does.

# Workflow inputs, referenced as ${{ inputs.<name> }}.
inputs:
  ticket_id:
    type: string
    required: true

# Steps form a DAG via `needs`; array order is for humans, execution order is the
# topological order. A step is EITHER scripted (`uses:` a built-in) or agentic
# (`prompt:` an .rebar/prompts/<id>.md). Pass values through `with:` and reference
# them by name — never inline ${{ }} into a prompt body.
steps:
  - id: fetch
    uses: fetch_ticket
    with:
      ticket_id: ${{ inputs.ticket_id }}

  - id: review
    prompt: code-quality
    needs: [fetch]
    with:
      context: ${{ steps.fetch.outputs.description }}
    output_schema: review_result
    mode: findings

  - id: gate
    uses: gate
    needs: [review]
    with:
      findings: ${{ steps.review.outputs.findings }}
      policy: default
"""


def _scaffold(name: str) -> str:
    """Return a valid skeleton workflow document for ``name``.

    Raises :class:`rebar.llm.errors.WorkflowParseError` if ``name`` is not a valid
    workflow id (the same lowercase pattern the schema enforces), so the failure is
    caught at authoring time rather than on the first validate.
    """
    from rebar.llm.errors import WorkflowParseError

    if not _SCAFFOLD_NAME_RE.match(name):
        raise WorkflowParseError(
            f"invalid workflow name {name!r}: use lowercase letters, digits, '-' and "
            f"'_' (must start with a letter)",
            source=name,
        )
    return _SCAFFOLD_V1.replace("__NAME__", name)


def _workflow(argv: list[str]) -> int:
    """``rebar workflow <new|validate|run|status|result>`` → the workflow toolchain.

    A native ``rebar.llm.workflow`` op intercepted in main() (like review/reconcile),
    so it owns its own ``--help``. ``new`` scaffolds; ``validate`` lints; ``run``
    executes (sync; ``--dry-run`` = offline FakeRunner, no tokens); ``status``/
    ``result`` read a run's state via replay. The ``show`` (render) arm is WS-I.
    """

    parser = _build_parser()

    try:
        args = parser.parse_args(argv)
    except ParseError as exc:
        return render_parse_error(exc)
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


def _workflow_edit(args: argparse.Namespace) -> int:
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


def _workflow_show(args: argparse.Namespace) -> int:
    from rebar.llm import errors as _werr
    from rebar.llm.workflow import render

    try:
        sys.stdout.write(render.render_workflow(args.file))
    except _werr.WorkflowError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    return 0


def _workflow_run(args: argparse.Namespace) -> int:
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


def _workflow_read(args: argparse.Namespace) -> int:
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


def _workflow_new(args: argparse.Namespace) -> int:
    from rebar import config
    from rebar.llm import errors as _werr
    from rebar.llm.workflow import lint as _lint

    try:
        content = _scaffold(args.name)
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


def _workflow_validate(args: argparse.Namespace) -> int:
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
    except Exception:  # noqa: BLE001 — not in a repo — skip the repo-scoped prompt-file lookup
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
