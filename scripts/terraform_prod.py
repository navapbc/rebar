#!/usr/bin/env python3
"""Mediated Terraform production plan/apply for ``infra/terraform``.

The production stack's safe source of truth is the tracked branch tip. This wrapper makes
that contract executable: it refuses to plan or apply unless the checkout is exactly current
with ``origin/main``, and it applies only a saved plan produced by the same wrapper from the
same commit.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"
DEFAULT_TERRAFORM_ROOT = REPO_ROOT / "infra" / "terraform"
_CURRENCY_GATE = REPO_ROOT / "scripts" / "check_terraform_tree_currency.py"


class UsageError(RuntimeError):
    """The requested wrapper operation would bypass the mediated apply contract."""


def _load_currency_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_terraform_tree_currency", _CURRENCY_GATE)
    if spec is None or spec.loader is None:  # pragma: no cover - importlib environment failure
        raise UsageError(f"cannot load terraform currency gate at {_CURRENCY_GATE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_terraform_tree_currency"] = module
    spec.loader.exec_module(module)
    return module


# raw-git-ok: executes terraform only; callers construct argv from the configured
# terraform binary plus literal terraform subcommands, never from git.
def _run(args: list[str], *, cwd: Path) -> int:
    proc = subprocess.run(args, cwd=cwd, check=False)  # raw-git-ok: terraform runner, not git
    return proc.returncode


# raw-git-ok: read-only plumbing (rev-parse) against the operator's code checkout,
# never the rebar tracker store; production state mutation happens through Terraform.
def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(  # raw-git-ok: read-only code-checkout plumbing
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise UsageError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _ensure_current(repo: Path, *, remote: str, branch: str, fetch: bool) -> str:
    gate = _load_currency_gate()
    verdict = gate.check(
        repo,
        remote=remote,
        branch=branch,
        mode=gate.MODE_TIP,
        fetch=fetch,
    )
    if verdict.code != gate.EXIT_CURRENT:
        for line in verdict.lines:
            print(line, file=sys.stderr)
        raise SystemExit(verdict.code)
    for line in verdict.lines:
        print(line)
    return _git(repo, "rev-parse", "HEAD")


def _metadata_path(plan_path: Path) -> Path:
    return plan_path.with_name(f"{plan_path.name}.rebar-meta.json")


def _normalise_remainder(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        return args[1:]
    return args


def _reject_unsafe_plan_args(args: list[str]) -> None:
    for arg in args:
        if arg == "-out" or arg.startswith("-out="):
            raise UsageError(
                "pass the saved plan path with wrapper option --out, not terraform -out"
            )


def _write_metadata(
    *,
    plan_path: Path,
    head: str,
    remote: str,
    branch: str,
    terraform_root: Path,
    terraform_args: list[str],
) -> None:
    metadata = {
        "head": head,
        "remote": remote,
        "branch": branch,
        "terraform_root": str(terraform_root),
        "terraform_args": terraform_args,
    }
    _metadata_path(plan_path).write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_metadata(plan_path: Path) -> dict[str, Any]:
    metadata_path = _metadata_path(plan_path)
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UsageError(
            f"{metadata_path} is missing; apply only plans created by "
            "`scripts/terraform_prod.py plan`"
        ) from exc
    except json.JSONDecodeError as exc:
        raise UsageError(f"{metadata_path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise UsageError(f"{metadata_path} must contain a JSON object")
    return data


def _init(terraform_bin: str, terraform_root: Path) -> int:
    return _run([terraform_bin, "init", "-input=false"], cwd=terraform_root)


def _plan(args: argparse.Namespace) -> int:
    terraform_args = _normalise_remainder(args.terraform_args)
    _reject_unsafe_plan_args(terraform_args)
    repo = args.repo.resolve()
    terraform_root = args.terraform_root.resolve()
    plan_path = args.out.resolve()

    head = _ensure_current(repo, remote=args.remote, branch=args.branch, fetch=args.fetch)
    init_rc = _init(args.terraform_bin, terraform_root)
    if init_rc != 0:
        return init_rc

    plan_rc = _run(
        [
            args.terraform_bin,
            "plan",
            "-input=false",
            "-out",
            str(plan_path),
            *terraform_args,
        ],
        cwd=terraform_root,
    )
    if plan_rc == 0:
        _write_metadata(
            plan_path=plan_path,
            head=head,
            remote=args.remote,
            branch=args.branch,
            terraform_root=terraform_root,
            terraform_args=terraform_args,
        )
    return plan_rc


def _apply(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    terraform_root = args.terraform_root.resolve()
    plan_path = args.plan.resolve()
    head = _ensure_current(repo, remote=args.remote, branch=args.branch, fetch=args.fetch)

    metadata = _read_metadata(plan_path)
    planned_head = metadata.get("head")
    if planned_head != head:
        raise UsageError(
            f"{plan_path} was planned from {planned_head!r}, but current HEAD is {head!r}; "
            "re-run `scripts/terraform_prod.py plan --out ...` from the current tracked tip"
        )
    planned_root = metadata.get("terraform_root")
    if planned_root != str(terraform_root):
        raise UsageError(
            f"{plan_path} was planned for terraform root {planned_root!r}, "
            f"not {str(terraform_root)!r}"
        )

    init_rc = _init(args.terraform_bin, terraform_root)
    if init_rc != 0:
        return init_rc
    return _run([args.terraform_bin, "apply", str(plan_path)], cwd=terraform_root)


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="repository root")
    parser.add_argument("--remote", default=DEFAULT_REMOTE, help="tracked remote")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="tracked branch")
    parser.add_argument(
        "--no-fetch",
        dest="fetch",
        action="store_false",
        help="do not refresh the remote ref before checking currency",
    )
    parser.add_argument(
        "--terraform-root",
        type=Path,
        default=DEFAULT_TERRAFORM_ROOT,
        help="Terraform production root",
    )
    parser.add_argument("--terraform-bin", default="terraform", help="Terraform executable")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan/apply the production Terraform root only from the tracked branch tip."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="create a saved production plan")
    _add_common_options(plan_parser)
    plan_parser.add_argument("--out", type=Path, required=True, help="saved plan path")
    plan_parser.add_argument(
        "terraform_args",
        nargs=argparse.REMAINDER,
        help="additional terraform plan arguments after --",
    )
    plan_parser.set_defaults(func=_plan)

    apply_parser = subparsers.add_parser("apply", help="apply a wrapper-created saved plan")
    _add_common_options(apply_parser)
    apply_parser.add_argument("plan", type=Path, help="saved plan path from the plan subcommand")
    apply_parser.set_defaults(func=_apply)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return int(args.func(args))
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
