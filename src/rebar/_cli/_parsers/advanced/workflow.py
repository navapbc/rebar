"""``rebar workflow`` parser factory (RP-05 S2c).

Reproduces the nested ``rebar workflow`` grammar (new / validate / run / show /
edit / status / result) from :mod:`rebar._cli._workflow_commands`, bound to a
caller-supplied ``prog``. Uses argparse's default help formatter (as the inline
parser did) so help renders byte-for-byte unchanged.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build(*, prog: str) -> argparse.ArgumentParser:
    """Build the workflow-toolchain parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        description="Author, validate, and run git-native workflows (.rebar/workflows/*.yaml).",
        formatter_class=argparse.HelpFormatter,
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
    p_run.add_argument(
        "--ticket",
        help="persist step effects to this ticket's event log; a later re-invocation "
        "with --run-id skips completed steps (resume-by-re-invocation, not auto-resume)",
    )
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

    return parser
