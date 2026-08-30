"""``python -m rebar.llm.evals.plan_replay tier0 --candidate <name> [--labels <path>]``
(ticket bouncy-peacockish-titmouse / 5d19-52e0-7c26-47fb).

Reads the tracker store roots from ``REBAR_ROOT`` (the checkout's own
``.tickets-tracker``) by default -- pass ``--store name=path`` (repeatable) to replay a
different store set."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from rebar.config import repo_root_or_none, tracker_dir
from rebar.llm.evals.plan_replay import report, tier0, tier1
from rebar.llm.evals.plan_replay.candidates import CANDIDATES
from rebar.llm.evals.plan_replay.verifier_candidates import load_verifier_candidate

_DEFAULT_CACHE_DIR = Path("docs/experiments/plan-review-gate/replay/cache")
_DEFAULT_OUT_DIR = Path("docs/experiments/plan-review-gate/replay")


def _default_store_roots() -> dict[str, str]:
    repo_root = repo_root_or_none() or os.getcwd()
    return {"rebar": str(tracker_dir(repo_root))}


def _parse_store(spec: str) -> tuple[str, str]:
    name, sep, path = spec.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(f"--store must be name=path, got {spec!r}")
    return name, path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rebar.llm.evals.plan_replay")
    subparsers = parser.add_subparsers(dest="command", required=True)

    tier0_parser = subparsers.add_parser("tier0", help="replay Pass-3 over the corpus")
    tier0_parser.add_argument(
        "--candidate",
        required=True,
        help=f"candidate name (known: {', '.join(sorted(CANDIDATES))})",
    )
    tier0_parser.add_argument("--labels", default=None, help="path to a labels-<hash>.jsonl file")
    tier0_parser.add_argument(
        "--store",
        action="append",
        default=[],
        type=_parse_store,
        help="name=path tracker store (repeatable); defaults to REBAR_ROOT's own tracker",
    )
    tier0_parser.add_argument("--cache-dir", default=str(_DEFAULT_CACHE_DIR))
    tier0_parser.add_argument("--out-dir", default=str(_DEFAULT_OUT_DIR))

    tier1_parser = subparsers.add_parser("tier1", help="replay Pass-2 over a corpus sample")
    tier1_parser.add_argument(
        "--candidate",
        default=None,
        help="path to a project-override verifier prompt; omit for the reproduction run",
    )
    tier1_parser.add_argument("--n", type=int, required=True, help="sample size")
    tier1_parser.add_argument("--seed", type=int, default=0, help="sampling seed")
    tier1_parser.add_argument(
        "--store",
        action="append",
        default=[],
        type=_parse_store,
        help="name=path tracker store (repeatable); defaults to REBAR_ROOT's own tracker",
    )
    tier1_parser.add_argument("--cache-dir", default=str(_DEFAULT_CACHE_DIR))
    tier1_parser.add_argument("--out-dir", default=str(_DEFAULT_OUT_DIR))

    return parser


def _run_tier0(args: argparse.Namespace) -> int:
    if args.candidate not in CANDIDATES:
        sys.stderr.write(
            f"error: unknown candidate {args.candidate!r}; known candidates: {sorted(CANDIDATES)}\n"
        )
        return 2

    store_roots = dict(args.store) if args.store else _default_store_roots()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    result = tier0.run_tier0(
        store_roots,
        cache_dir=args.cache_dir,
        candidate_name=args.candidate,
        labels_path=args.labels,
    )

    report_path = out_dir / f"tier0-{args.candidate}-{result['content_hash'][:12]}.md"
    report_path.write_text(report.render_report(result), encoding="utf-8")
    sys.stdout.write(
        f"wrote {report_path}\n"
        f"replayed {result['row_count']} rows "
        f"(skipped {result['skipped']}), "
        f"self-check mismatches: {result['flip_matrix']['self_check_mismatches']}\n"
    )
    return 0


def _run_tier1(args: argparse.Namespace) -> int:
    try:
        candidate = load_verifier_candidate(args.candidate)
    except FileNotFoundError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    store_roots = dict(args.store) if args.store else _default_store_roots()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    candidate_name = args.candidate or "current"

    result = tier1.run_tier1(
        store_roots,
        cache_dir=args.cache_dir,
        candidate=candidate,
        candidate_name=candidate_name,
        n=args.n,
        seed=args.seed,
    )

    report_path = out_dir / f"tier1-{candidate_name.replace('/', '_')}-{result['run_id']}.md"
    report_path.write_text(tier1.render_tier1_report(result), encoding="utf-8")
    sys.stdout.write(
        f"wrote {report_path}\n"
        f"sampled {result['sample_n']} of {result['requested_n']} requested, "
        f"ledger cost ${result['ledger_entry']['usd']:.2f}\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "tier0":
        return _run_tier0(args)
    if args.command == "tier1":
        return _run_tier1(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
