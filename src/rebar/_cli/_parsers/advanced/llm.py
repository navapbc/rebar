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
    if ref_configurable:
        ref_help = (
            f"a branch, tag, or commit SHA to verify against (default: {ref_default}, "
            "configurable through REBAR_GATE_REF or [snapshot].ref). Pass --ref HEAD when "
            "the review depends on code you have committed locally but have not yet landed "
            "on the default ref, such as a stacked change or feature branch. The default "
            "ref reads a snapshot that predates that code, so symbols it adds report as "
            "'does not exist' false findings"
        )
    else:
        ref_help = f"a branch, tag, or commit SHA to verify against (default: {ref_default})"
    parser.add_argument("--ref", default=None, help=ref_help)
    parser.add_argument(
        "--source",
        choices=["attested", "local"],
        default=None,
        help="attested (default) verifies a snapshot pinned at --ref, signs the result, "
        "and records verified_at_sha. local reads the in-place checkout, allows a dirty "
        "tree, and never signs",
    )


def build_review_code(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar review-code`` parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        description="Run an LLM code review of a change (a git range or a diff file) and "
        "emit aggregated structured findings. Requires the 'agents' extra and an API key.",
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument("--base", default="HEAD~1", help="base git ref (default HEAD~1)")
    parser.add_argument("--head", default="HEAD", help="head git ref (default HEAD)")
    parser.add_argument("--diff-file", help="review this unified-diff file instead of a git range")
    parser.add_argument(
        "--reviewer",
        action="append",
        dest="reviewers",
        help="reviewer id, repeatable (default: deterministic selection)",
    )
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    _add_ref_source(parser, ref_default="the reviewed --head", ref_configurable=False)
    return parser


def build_scan_spec(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar scan-spec`` parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        description="Batch-scan open epics against a specification and emit "
        "structured findings (gaps, conflicts, and overlaps). Requires the 'agents' extra.",
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument("--spec-file", required=True, help="path to the specification text")
    parser.add_argument("--batch-size", type=int, default=5, help="epics per batch (default 5)")
    parser.add_argument(
        "--epic",
        action="append",
        dest="epics",
        help="restrict to these epic ids, repeatable (default: all open epics)",
    )
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    _add_ref_source(parser)
    return parser


def build_verify_completion(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar verify-completion`` parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        description="Run the completion-verifier agent on a ticket and emit a PASS or FAIL "
        "verdict for whether its completion requirements are demonstrably met by the "
        "implementation. Completion requirements are the acceptance, success, and close "
        "criteria and the definitions of done, and for a bug, that the bug is resolved. "
        "Requires the 'agents' extra and a model API key. Run `rebar verify-completion "
        "--check` to confirm availability.",
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument("ticket_id", nargs="?", help="ticket id, short id, or alias")
    parser.add_argument(
        "--graph",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="include the ticket's descendants. Use --no-graph to force own-criteria "
        "verification (default: auto, on for epics and off otherwise)",
    )
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    parser.add_argument(
        "--check", action="store_true", help="print backend/credential availability and exit"
    )
    parser.add_argument(
        "--no-sign",
        action="store_true",
        help="run the verifier but do not sign a reusable completion-verifier attestation. "
        "By default an attested PASS (source=attested) signs that attestation, which a later "
        "same-ref `rebar transition ... closed` reuses to skip a duplicate billable verifier "
        "run, so this flag is the explicit opt-out. The COMPLETION_VERDICT sidecar is still "
        "emitted for both PASS and FAIL, and only the signature is skipped. A local verdict "
        "(--source local) is never signed",
    )
    _add_ref_source(parser)
    return parser


def build_explain(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar explain`` parser bound to ``prog``."""
    from rebar.llm.plan_review import registry

    guides = ", ".join(sorted(registry.AUTHOR_GUIDES))
    parser = build_argument_parser(
        prog=prog,
        description="Print a plan-review criterion's authoring-guide section (for example "
        f"`rebar explain F1`), or an author-facing prose guide ({guides}). For example, "
        "`rebar explain plan` explains how to write a plan that passes the plan-review gate. "
        "One shared lookup with the MCP explain_criterion tool.",
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument(
        "topic",
        nargs="?",
        help=f"a plan-review criterion id (for example F1 or G3) or a guide ({guides})",
    )
    return parser


def build_review_plan(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar review-plan`` parser bound to ``prog``."""
    from rebar import config

    parser = build_argument_parser(
        prog=prog,
        description="Run the plan-review gate on a ticket. The gate applies a deterministic "
        "Layer-1 floor and a four-pass review of the plan (find, verify, decide, then coach), "
        "then signs a plan-review attestation on a non-blocking PASS. It is the inverse of "
        "verify-completion.",
        epilog=(
            "Coaching deep-links and `rebar explain <criterion-id>` reference the criteria "
            f"authoring guide at {config.plan_review_docs_url()} "
            "(anchor `#<criterion-id lower-cased>`, override the base with REBAR_DOCS_URL)."
        ),
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument("ticket_id", nargs="?", help="ticket id, short id, or alias")
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    parser.add_argument(
        "--no-sign",
        action="store_true",
        help="run the review but do not sign an attestation. By default a non-blocking PASS "
        "signs one. That attestation is the durable product of the review, and it is what the "
        "claim gate consumes, so this flag is the explicit opt-out rather than the way to get "
        "a signature. An unsigned PASS leaves the claim gate unsatisfied. Recover a lost "
        "signature cheaply, with no LLM call, using `rebar sign-review <id>`",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-run the review even if a current attestation exists, bypassing the "
        "idempotence short-circuit. It also reviews a ticket that is not yet claimable "
        "(closed, idea, or blocked status, or blocked by an unclosed dependency), which is "
        "otherwise fast-failed without running the LLM",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="print backend and credential availability and exit. This does not inspect a "
        "ticket's attestation status. Use --status for that",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="report read-only whether the ticket's plan-review attestation is current right "
        "now, with no model call, no network, and no re-sign. Prints the verdict and the bound "
        "verified-at-sha. Exit 0 when current and 12 when stale or absent",
    )
    parser.add_argument(
        "--retry",
        action="store_true",
        help="resume only the exact latest eligible INDETERMINATE review. It reuses the "
        "checkpointed findings of the already-successful units and issues model calls only "
        "for the missing units, under a fresh per-invocation attempt budget. It is eligible "
        "only when the latest retained REVIEW_RESULT is INDETERMINATE with a versioned "
        "discovery journal and at least one retryable missing unit. A PASS or BLOCK verdict, a "
        "non-retryable indeterminate, or a missing, legacy, corrupt, stale, or "
        "digest-mismatched journal is refused before any model call (exit 2) with the normal "
        "full-review remedy. Cumulative retry lineage is recorded as audit telemetry and is "
        "never enforced as a cap. This flag is mutually exclusive with --force, --status, and "
        "--check, and is compatible with --no-sign. The retry response stays a narrow "
        "end-result view, and the per-unit journal is never printed",
    )
    _add_ref_source(parser)
    return parser


def build_sign_review(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar sign-review`` parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        description="Persist the plan-review attestation again, cheaply, for an "
        "already-computed and still-valid PASS verdict from the latest REVIEW_RESULT sidecar, "
        "without re-running the multi-pass LLM review. Use it to recover a signature that "
        "`rebar review-plan` computed but failed to persist, for example after a transient "
        "git index.lock. It refuses to sign a non-PASS verdict or a verdict that is stale "
        "because the plan changed since the review.",
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument("ticket_id", nargs="?", help="ticket id, short id, or alias")
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    return parser
