#!/usr/bin/env python3
"""Provide testable release preflight guards for the publishing workflow.

The guards require version lockstep across ``pyproject.toml`` plus the top-level and
package versions in ``server.json``. They also require an MCP Registry description of at
most 100 characters, release ancestry from ``origin/main``, and a protected ``pypi``
environment restricted to ``main``. Validation guards return failure strings.
``is_ancestor`` returns a Boolean and raises on other Git errors. The CLI writes failures
to stderr and exits with zero only when its selected check passes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import tomllib


def check_version_lockstep(version: str, pyproject_text: str, server_json: dict) -> list[str]:
    """Report version mismatches and reject an absent or empty package list."""
    failures: list[str] = []

    pyproject = tomllib.loads(pyproject_text)
    py_version = pyproject.get("project", {}).get("version")
    if py_version is None:
        failures.append(f"pyproject [project].version is absent (expected {version!r})")
    elif py_version != version:
        failures.append(f"pyproject version {py_version!r} != release version {version!r}")

    top = server_json.get("version")
    if top != version:
        failures.append(f"server.json version {top!r} != release version {version!r}")

    packages = server_json.get("packages")
    if not packages:
        failures.append("server.json has no packages entries (malformed for lockstep)")
    else:
        for index, pkg in enumerate(packages):
            pkg_version = pkg.get("version")
            if pkg_version != version:
                failures.append(
                    f"server.json packages[{index}] version {pkg_version!r} != "
                    f"release version {version!r}"
                )

    return failures


MCP_REGISTRY_DESCRIPTION_MAX_LEN = 100


def check_mcp_registry_description(server_json: dict) -> list[str]:
    """Report a missing or overlength top-level MCP Registry description.

    The registry rejects an overlength description after the PyPI publish step, so this
    guard must run before publishing.
    """
    description = server_json.get("description")
    if description is None:
        return ["server.json has no top-level description field"]
    if len(description) > MCP_REGISTRY_DESCRIPTION_MAX_LEN:
        return [
            f"server.json description is {len(description)} chars, exceeds the MCP "
            f"Registry's {MCP_REGISTRY_DESCRIPTION_MAX_LEN}-char limit: {description!r}"
        ]
    return []


def is_ancestor(sha: str, ref: str, *, cwd: str | Path | None = None) -> bool:
    """Return ancestry for exit codes zero and one, and raise on other Git failures."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, ref],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise subprocess.CalledProcessError(
        result.returncode,
        result.args,
        output=result.stdout,
        stderr=result.stderr,
    )


def check_env_protection(env: dict, branch_policies: dict | None = None) -> list[str]:
    """Require reviewers and a deployment policy that allows only ``main``."""
    failures: list[str] = []

    rules = env.get("protection_rules") or []
    reviewer_rule = next((r for r in rules if r.get("type") == "required_reviewers"), None)
    if reviewer_rule is None:
        failures.append("no required_reviewers protection rule (deploys need no review)")
    elif not reviewer_rule.get("reviewers"):
        failures.append("required_reviewers rule lists no reviewers (deploys need no review)")

    if env.get("deployment_branch_policy") is None:
        failures.append(
            "deployment_branch_policy is null: every branch may deploy (must "
            "restrict to the main branch)"
        )

    if branch_policies is not None:
        names = {p.get("name") for p in branch_policies.get("branch_policies") or []}
        if names != {"main"}:
            failures.append(
                f"deployment branch policies {sorted(names)} are not exactly "
                "['main'] (only the main branch may deploy)"
            )

    return failures


def _cmd_version_lockstep(args: argparse.Namespace) -> list[str]:
    pyproject_text = Path(args.pyproject).read_text(encoding="utf-8")
    server_json = json.loads(Path(args.server_json).read_text(encoding="utf-8"))
    return check_version_lockstep(args.version, pyproject_text, server_json)


def _cmd_mcp_registry_description(args: argparse.Namespace) -> list[str]:
    server_json = json.loads(Path(args.server_json).read_text(encoding="utf-8"))
    return check_mcp_registry_description(server_json)


def _cmd_ancestry(args: argparse.Namespace) -> list[str]:
    if is_ancestor(args.sha, args.ref):
        return []
    return [f"{args.sha} is not an ancestor of {args.ref}"]


def _cmd_env_preflight(args: argparse.Namespace) -> list[str]:
    env = json.loads(Path(args.response).read_text(encoding="utf-8"))
    branch_policies = None
    if args.branch_policies:
        branch_policies = json.loads(Path(args.branch_policies).read_text(encoding="utf-8"))
    return check_env_protection(env, branch_policies)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_ver = sub.add_parser("version-lockstep", help="assert pyproject/server.json versions match")
    p_ver.add_argument("--version", required=True)
    p_ver.add_argument("--pyproject", required=True)
    p_ver.add_argument("--server-json", required=True)
    p_ver.set_defaults(func=_cmd_version_lockstep)

    p_desc = sub.add_parser(
        "mcp-registry-description",
        help="assert server.json's top-level description fits the MCP Registry schema",
    )
    p_desc.add_argument("--server-json", required=True)
    p_desc.set_defaults(func=_cmd_mcp_registry_description)

    p_anc = sub.add_parser("ancestry", help="assert a commit descends from a ref")
    p_anc.add_argument("--sha", required=True)
    p_anc.add_argument("--ref", default="origin/main")
    p_anc.set_defaults(func=_cmd_ancestry)

    p_env = sub.add_parser("env-preflight", help="assert the pypi environment gates deploys")
    p_env.add_argument("--response", required=True)
    p_env.add_argument("--branch-policies")
    p_env.set_defaults(func=_cmd_env_preflight)

    args = parser.parse_args(argv)
    failures = args.func(args)
    if failures:
        for line in failures:
            print(f"release_guards: {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
