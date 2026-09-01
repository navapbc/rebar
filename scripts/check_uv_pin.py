#!/usr/bin/env python3
"""Single-source gate for the uv version pin [rebar:56b7-b21a-c8ab-4afc].

``astral-sh/setup-uv`` was once SHA-pinned at every call site, but the action still resolved
and downloaded uv through a remote manifest. rebar now owns a repository-local setup action
that reads the exact ``[tool.uv] required-version`` from ``pyproject.toml`` and downloads the
version-pinned release artifact directly, guarded by committed SHA-256 checksums.

This gate keeps that contract single-sourced and manifest-free by failing the build on the
ways it can be defeated:

1. **Removed** — the ``[tool.uv] required-version`` key is gone, so there is no repository pin.
2. **Loosened to a range** — ranges are not an unambiguous exact pin for every reader.
3. **Overridden per call site** — a ``version`` or ``version-file`` input would silently diverge
   that job from ``pyproject.toml``.
4. **Shadowed by a root uv.toml** — uv treats ``uv.toml`` as a replacement for ``[tool.uv]``.
5. **Bypassed through the upstream action** — any workflow using ``astral-sh/setup-uv`` regains
   the manifest fetch.
6. **Weakened local action** — the committed action must exist, avoid manifest endpoints, and
   carry the exact checksums for every supported runner asset.

Stdlib + PyYAML only, with no CI provider required: it runs from ``make lint`` on a developer
laptop exactly as it runs in CI, which is the portability contract every gate here holds to.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The upstream action this repository-local action replaces. Matched against the ``uses:``
#: value before the ``@``, so it is independent of which SHA a call site pins.
SETUP_UV_ACTION = "astral-sh/setup-uv"
LOCAL_SETUP_UV_ACTION = "./.github/actions/setup-uv"
LOCAL_SETUP_UV_ACTION_FILE = Path(".github/actions/setup-uv/action.yml")

#: Inputs that override the workspace scan. ``version`` is honoured by
#: ``ExplicitInputVersionResolver`` and ``version-file`` by ``VersionFileVersionResolver``,
#: both of which are ordered ahead of ``WorkspaceVersionResolver`` in the upstream action.
OVERRIDING_INPUTS = ("version", "version-file")
FORBIDDEN_ACTION_TEXT = ("raw.githubusercontent.com", "Fetching manifest data")
REQUIRED_CHECKSUMS = {
    "UV_SHA256_X86_64_UNKNOWN_LINUX_GNU": (
        "788f18abea7c5f55d6216e4f5613fd89d4d59b631efeec117b2b07fe72f1da21"
    ),
    "UV_SHA256_AARCH64_UNKNOWN_LINUX_GNU": (
        "66393193038dd7eb108abd7a218d9cec04ac70ab98242b0720fa94de19223b7c"
    ),
    "UV_SHA256_X86_64_APPLE_DARWIN": (
        "06b8ae1da8c2661c5434507a66f8c2b0b835933bf955b5958a9ac357a37d1959"
    ),
    "UV_SHA256_AARCH64_APPLE_DARWIN": (
        "127ebdda7ad953cdf198e964b570ea5771b85467ea93eb7cb6d6f8e6f55408f3"
    ),
    "UV_SHA256_X86_64_PC_WINDOWS_MSVC": (
        "bf1518af459a3915511a11fdc6e2f43ef9a2afa138b9d498eeb9642fe9d85218"
    ),
    "UV_SHA256_AARCH64_PC_WINDOWS_MSVC": (
        "1611d0f4be72b0a354ad9a6ae954093dd4c91e93e36b8b490326a05a039ffe14"
    ),
}


@dataclass(frozen=True)
class Finding:
    """One way the single-source pin has been defeated."""

    location: str
    detail: str

    def render(self) -> str:
        return f"{self.location}: {self.detail}"


def _is_exact_specifier(specifier: str) -> bool:
    """Accept only the explicit ``==X.Y.Z`` form shared by every uv reader."""
    stripped = specifier.strip()
    if not stripped.startswith("=="):
        return False
    remainder = stripped[2:].strip()
    if not remainder or "," in remainder or "*" in remainder:
        return False
    if not remainder[0].isdigit():
        return False
    return all(part.isdigit() for part in remainder.split(".") if part != "")


def check_pyproject(root: Path) -> list[Finding]:
    """Assert ``pyproject.toml`` carries an EXACT ``[tool.uv] required-version``."""
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return [Finding("pyproject.toml", "missing — the uv pin has nowhere to live")]

    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - malformed tree
        return [Finding("pyproject.toml", f"could not be parsed as TOML: {exc}")]

    required = data.get("tool", {}).get("uv", {}).get("required-version")
    if required is None:
        return [
            Finding(
                "pyproject.toml",
                "no [tool.uv] required-version — the local setup-uv action has no "
                "single-sourced uv release to install",
            )
        ]
    if not isinstance(required, str) or not _is_exact_specifier(required):
        return [
            Finding(
                "pyproject.toml",
                f'[tool.uv] required-version is {required!r}, which is not an exact "==X.Y.Z" '
                "pin. Keep uv pinned exactly once in pyproject.toml",
            )
        ]
    return []


def check_no_root_uv_toml(root: Path) -> list[Finding]:
    """Assert no root ``uv.toml`` shadows the ``pyproject.toml`` pin."""
    if (root / "uv.toml").exists():
        return [
            Finding(
                "uv.toml",
                "a root uv.toml shadows [tool.uv] in pyproject.toml for uv itself. Keep "
                "the pin in pyproject.toml and delete this file",
            )
        ]
    return []


def _steps(document: Any) -> list[dict[str, Any]]:
    """Yield every step mapping in a parsed workflow, across all jobs."""
    steps: list[dict[str, Any]] = []
    if not isinstance(document, dict):
        return steps
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        return steps
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict):
                steps.append(step)
    return steps


def _action_name(uses: str) -> str:
    """Return the action path/name without any version suffix."""
    return uses.split("@", 1)[0]


def _check_local_setup_uv_inputs(
    path: Path, root: Path, step: dict[str, Any], uses: str
) -> list[Finding]:
    inputs = step.get("with") or {}
    if not isinstance(inputs, dict):
        return []

    findings: list[Finding] = []
    for name in OVERRIDING_INPUTS:
        if name in inputs:
            findings.append(
                Finding(
                    f"{path.relative_to(root)} ({step.get('name') or uses})",
                    f"local setup-uv step sets '{name}: {inputs[name]}'. That would override "
                    "the single source in [tool.uv] required-version; remove the input",
                )
            )
    return findings


def check_workflows(root: Path) -> list[Finding]:
    """Assert workflows use only the local setup-uv action, with no version override."""
    findings: list[Finding] = []
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return findings

    for path in sorted(workflows.iterdir()):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:  # pragma: no cover - actionlint owns parse errors
            findings.append(Finding(str(path.relative_to(root)), f"could not be parsed: {exc}"))
            continue

        for step in _steps(document):
            uses = step.get("uses")
            if not isinstance(uses, str):
                continue
            action = _action_name(uses)
            if action == SETUP_UV_ACTION:
                findings.append(
                    Finding(
                        f"{path.relative_to(root)} ({step.get('name') or uses})",
                        f"uses {SETUP_UV_ACTION}, which fetches the uv manifest. Use "
                        f"{LOCAL_SETUP_UV_ACTION} instead",
                    )
                )
            elif action == LOCAL_SETUP_UV_ACTION:
                findings.extend(_check_local_setup_uv_inputs(path, root, step, uses))
    return findings


def _load_action(root: Path) -> tuple[dict[str, Any] | None, str, list[Finding]]:
    action_path = root / LOCAL_SETUP_UV_ACTION_FILE
    location = str(LOCAL_SETUP_UV_ACTION_FILE)
    if not action_path.is_file():
        return None, "", [Finding(location, "missing — workflows cannot install uv locally")]

    text = action_path.read_text(encoding="utf-8")
    findings = [
        Finding(location, f"contains {token!r}; the local action must not fetch the uv manifest")
        for token in FORBIDDEN_ACTION_TEXT
        if token in text
    ]
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, text, [*findings, Finding(location, f"could not be parsed: {exc}")]
    if not isinstance(document, dict):
        return None, text, [*findings, Finding(location, "must be a YAML mapping")]
    return document, text, findings


def _action_env(document: dict[str, Any]) -> dict[str, str]:
    runs = document.get("runs")
    if not isinstance(runs, dict):
        return {}
    steps = runs.get("steps")
    if not isinstance(steps, list):
        return {}

    env: dict[str, str] = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_env = step.get("env")
        if not isinstance(step_env, dict):
            continue
        for name, value in step_env.items():
            if isinstance(name, str) and isinstance(value, str):
                env[name] = value
    return env


def _has_cache_step(document: dict[str, Any]) -> bool:
    runs = document.get("runs")
    if not isinstance(runs, dict):
        return False
    steps = runs.get("steps")
    if not isinstance(steps, list):
        return False
    return any(
        isinstance(step, dict)
        and isinstance(step.get("uses"), str)
        and _action_name(step["uses"]) == "actions/cache"
        for step in steps
    )


def check_local_action(root: Path) -> list[Finding]:
    """Assert the committed local action preserves the manifest-free uv install contract."""
    document, _text, findings = _load_action(root)
    if document is None:
        return findings

    location = str(LOCAL_SETUP_UV_ACTION_FILE)
    inputs = document.get("inputs")
    if not isinstance(inputs, dict) or "enable-cache" not in inputs:
        findings.append(Finding(location, "must define the enable-cache input used by workflows"))
    if not _has_cache_step(document):
        findings.append(Finding(location, "must wire enable-cache through actions/cache"))

    env = _action_env(document)
    for name, expected in REQUIRED_CHECKSUMS.items():
        actual = env.get(name)
        if actual is None:
            findings.append(Finding(location, f"missing checksum env entry {name}"))
        elif len(actual) != 64 or any(char not in "0123456789abcdef" for char in actual):
            findings.append(
                Finding(location, f"checksum env entry {name} is malformed: {actual!r}")
            )
        elif actual != expected:
            findings.append(
                Finding(location, f"checksum env entry {name} is {actual}, expected {expected}")
            )
    return findings


def check_repo(root: Path) -> list[Finding]:
    """Run all uv-pin assertions against ``root``."""
    return (
        check_pyproject(root)
        + check_no_root_uv_toml(root)
        + check_local_action(root)
        + check_workflows(root)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate the single-sourced uv version pin.")
    parser.add_argument("--root", default=str(REPO_ROOT), help="repo root to scan")
    args = parser.parse_args(argv)

    findings = check_repo(Path(args.root))
    if not findings:
        return 0
    for finding in findings:
        print(f"check_uv_pin: {finding.render()}", file=sys.stderr)
    print(
        f"\ncheck_uv_pin: {len(findings)} finding(s). uv must be pinned exactly ONCE, as "
        '[tool.uv] required-version = "==X.Y.Z" in pyproject.toml, and every workflow must '
        f"install it through {LOCAL_SETUP_UV_ACTION} without version overrides or manifest "
        "fetches.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
