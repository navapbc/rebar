"""LLM / agent-operation parser factories (RP-05 S2c).

Lean, prog-bound factories for the flat ``rebar`` LLM commands — ``review-code`` /
``scan-spec`` / ``verify-completion`` / ``explain`` / ``review-plan`` /
``sign-review`` — reproducing the current argument surfaces from
:mod:`rebar._cli._llm_commands`, including the shared ``--ref`` / ``--source``
controls. Each uses argparse's default help formatter (as the inline parsers did).

Only the stdlib and :mod:`rebar._cli._parser` are imported at module top-level;
the small amount of registry/config metadata a couple of factories embed in help
text is imported lazily inside the relevant ``build_*`` function (never the heavy
LLM runtime, which stays in the handlers).
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


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


def build_review_code(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar review-code`` parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        description="Run an LLM code review of a change (git range or diff file) and "
        "emit aggregated structured findings. Needs the 'agents' extra + an API key.",
        formatter_class=argparse.HelpFormatter,
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
    return parser


def build_scan_spec(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar scan-spec`` parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        description="Batch-scan open epics against a specification and emit "
        "structured findings (gaps/conflicts/overlaps). Needs the 'agents' extra.",
        formatter_class=argparse.HelpFormatter,
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
    return parser


def build_verify_completion(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar verify-completion`` parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        description="Run the completion-verifier agent on a ticket and emit a PASS/FAIL verdict "
        "that its completion requirements (acceptance/success/close criteria, definitions of "
        "done; for bugs, that the bug is resolved) are demonstrably met by the implementation. "
        "Needs the 'agents' extra + a model API key; see `rebar verify-completion --check`.",
        formatter_class=argparse.HelpFormatter,
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
    return parser


def build_explain(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar explain`` parser bound to ``prog``."""
    from rebar.llm.plan_review import registry

    guides = ", ".join(sorted(registry.AUTHOR_GUIDES))
    parser = build_argument_parser(
        prog=prog,
        description="Print a plan-review criterion's authoring-guide section (e.g. `rebar explain "
        f"F1`), or an author-facing prose guide ({guides}) — e.g. `rebar explain plan` for how to "
        "write a plan that passes the plan-review gate. One shared lookup with the MCP "
        "explain_criterion tool.",
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument(
        "topic", nargs="?", help=f"a plan-review criterion id (e.g. F1, G3) or a guide ({guides})"
    )
    return parser


def build_review_plan(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar review-plan`` parser bound to ``prog``."""
    from rebar import config

    parser = build_argument_parser(
        prog=prog,
        description="Run the plan-review gate on a ticket: a deterministic Layer-1 floor + a "
        "four-pass (find → verify → decide → coach) review of the plan, then sign a "
        "plan-review attestation on a non-blocking PASS. The inverse of verify-completion.",
        epilog=(
            "Coaching deep-links + `rebar explain <criterion-id>` reference the criteria "
            f"authoring guide at {config.plan_review_docs_url()} "
            "(anchor `#<criterion-id lower-cased>`; override the base with REBAR_DOCS_URL)."
        ),
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument("ticket_id", nargs="?", help="ticket id, short id, or alias")
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    parser.add_argument(
        "--no-sign",
        action="store_true",
        help="run the review but do NOT sign an attestation. By default a non-blocking PASS "
        "SIGNS one — that attestation is the review's durable product, and it is what the "
        "claim gate consumes — so this flag is the explicit opt-out, not the way to get a "
        "signature. An unsigned PASS leaves the claim gate unsatisfied; recover a lost "
        "signature cheaply (no LLM) with `rebar sign-review <id>`",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-run the review even if a current attestation exists "
        "(bypass the idempotence short-circuit); also reviews a ticket that is not "
        "yet claimable (closed/idea/blocked status, or blocked by an unclosed "
        "dependency), which is otherwise fast-failed without running the LLM",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="print backend/credential availability and exit; does NOT inspect a ticket's "
        "attestation status (for that, use --status)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="read-only: report whether the ticket's plan-review attestation is CURRENT right "
        "now (no model call, no network, no re-sign); prints the verdict + bound verified-at-sha. "
        "Exit 0 when current, 12 when stale/absent",
    )
    _add_ref_source(parser)
    return parser


def build_sign_review(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar sign-review`` parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        description="Cheaply (re)persist the plan-review attestation for an already-computed, "
        "still-valid PASS verdict from the latest REVIEW_RESULT sidecar — WITHOUT re-running the "
        "multi-pass LLM review. Use it to recover a signature that a `rebar review-plan` computed "
        "but failed to persist (e.g. a transient git index.lock). Refuses to sign a non-PASS or a "
        "verdict that is stale because the plan changed since the review.",
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument("ticket_id", nargs="?", help="ticket id, short id, or alias")
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    return parser
