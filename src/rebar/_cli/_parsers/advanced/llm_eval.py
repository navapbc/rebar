"""LLM eval / config parser factories (RP-05 S2c).

Lean, prog-bound factories for the nested ``rebar prompt`` / ``rebar criteria`` /
``rebar llm`` command families, reproducing the grammars from
:mod:`rebar._cli._llm_eval_commands`. Each uses argparse's default help formatter
(as the inline parsers did) and imports only the stdlib and
:mod:`rebar._cli._parser`.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build_prompt(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar prompt`` parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        description="Evaluate git-canonical prompts.",
        formatter_class=argparse.HelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="cmd")
    p_eval = subparsers.add_parser("eval", help="validate + summarize a prompt's eval spec")
    p_eval.add_argument("prompt_id", help="prompt/reviewer id (e.g. code-quality)")
    p_eval.add_argument("--output", "-o", choices=["text", "json"], default="text")
    return parser


def build_criteria(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar criteria`` parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        description="Evaluate + calibrate review criteria.",
        formatter_class=argparse.HelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="cmd")
    p_eval = subparsers.add_parser("eval", help="run a criterion's calibration fixtures live")
    p_eval.add_argument(
        "criterion_id",
        nargs="?",
        default=None,
        help="criterion id (e.g. F1, project.no_print; code-review: project.foo)",
    )
    p_eval.add_argument(
        "--changed-since",
        metavar="REF",
        help="print plan-review criterion ids whose rubric changed since REF",
    )
    p_eval.add_argument(
        "--require-live",
        action="store_true",
        help="with --changed-since: fail (non-zero) instead of exiting 0 when selected "
        "criteria cannot be run live because no LLM backend/credentials are available",
    )
    p_eval.add_argument(
        "--runs", type=int, default=1, help="N-run stability: runs per fixture (default 1)"
    )
    p_eval.add_argument("--output", "-o", choices=["text", "json"], default="text")
    p_heal = subparsers.add_parser("heal", help="mine regression fixtures for gap criteria")
    p_heal.add_argument(
        "--dry-run",
        action="store_true",
        help="print the attempt list without running or spending",
    )
    return parser


def build_llm(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar llm`` parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        description="Configure and check the rebar LLM framework.",
        formatter_class=argparse.HelpFormatter,
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
    return parser
