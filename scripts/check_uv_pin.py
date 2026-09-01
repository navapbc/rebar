#!/usr/bin/env python3
"""Single-source gate for the uv version pin [rebar:56b7-b21a-c8ab-4afc].

``astral-sh/setup-uv`` is SHA-pinned at all 25 call sites, but for a long time **uv itself**
was not pinned. With no locally-determinable version the action fell back to resolving one from
a remote manifest, so every job acquired a network dependency it did not need — and when that
fetch failed, the job failed with ``##[error]fetch failed``, a verdict with no relationship to
the change under test. That is what killed run 33214025855 after 21 seconds.

The fix is one line, ``[tool.uv] required-version`` in ``pyproject.toml``. This gate keeps that
line the SINGLE SOURCE it claims to be, by failing the build on the four ways it can be defeated:

1. **Removed** — the section or the key is gone, so the fallback fetch returns for every job.
2. **Loosened to a range** — the subtle one, and the reason a mere presence check is not enough.
   The action strips a leading ``==`` and classifies the remainder with ``tc.isExplicitVersion``
   (``src/version/specifier.ts``). ``==X.Y.Z`` is *exact* and resolves with no network; a range
   like ``>=X.Y.Z`` takes the range branch and STILL fetches the manifest. A range therefore
   looks pinned to a reader while restoring the exact failure mode the pin removed.
3. **Overridden per call site** — ``ExplicitInputVersionResolver`` runs BEFORE the workspace
   scan (``src/version/version-request-resolver.ts``), so a ``version:`` or ``version-file:``
   input on any one step silently diverges that job from the project pin while every other job
   still honours it. This is the drift the ticket's third acceptance criterion is about.
4. **Shadowed by a root uv.toml** — the workspace scan reads ``uv.toml`` *before*
   ``pyproject.toml``, and uv itself treats a ``uv.toml`` as a REPLACEMENT for ``[tool.uv]``
   rather than a merge. A root ``uv.toml`` would therefore take over both readers at once.

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

#: The action whose version resolution this gate governs. Matched against the ``uses:`` value
#: before the ``@``, so it is independent of which SHA the call site pins.
SETUP_UV_ACTION = "astral-sh/setup-uv"

#: Inputs that override the workspace scan. ``version`` is honoured by
#: ``ExplicitInputVersionResolver`` and ``version-file`` by ``VersionFileVersionResolver``,
#: both of which are ordered ahead of ``WorkspaceVersionResolver``.
OVERRIDING_INPUTS = ("version", "version-file")


@dataclass(frozen=True)
class Finding:
    """One way the single-source pin has been defeated."""

    location: str
    detail: str

    def render(self) -> str:
        return f"{self.location}: {self.detail}"


def _is_exact_specifier(specifier: str) -> bool:
    """Mirror the action's own exact-vs-range classification.

    ``normalizeVersionSpecifier`` strips a leading ``==`` and ``tc.isExplicitVersion`` then
    accepts a bare dotted release. Anything carrying a comparison operator after that strip
    (``>=``, ``~=``, ``<``, ``!=``, ``*``, or a comma-separated clause) is a range, which
    resolves through the manifest fetch this pin exists to remove.
    """
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
                "no [tool.uv] required-version — setup-uv falls back to fetching a remote "
                "manifest on EVERY job, which is bug 56b7-b21a-c8ab-4afc",
            )
        ]
    if not isinstance(required, str) or not _is_exact_specifier(required):
        return [
            Finding(
                "pyproject.toml",
                f'[tool.uv] required-version is {required!r}, which is not an exact "==X.Y.Z" '
                "pin. Only an exact version resolves without a network call; a range still "
                "fetches the manifest, so this reads as pinned while restoring the outage",
            )
        ]
    return []


def check_no_root_uv_toml(root: Path) -> list[Finding]:
    """Assert no root ``uv.toml`` shadows the ``pyproject.toml`` pin."""
    if (root / "uv.toml").exists():
        return [
            Finding(
                "uv.toml",
                "a root uv.toml shadows [tool.uv] in pyproject.toml for BOTH setup-uv (which "
                "scans uv.toml first) and uv itself (which treats it as a replacement, not a "
                "merge). Keep the pin in pyproject.toml and delete this file",
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


def check_workflows(root: Path) -> list[Finding]:
    """Assert no ``setup-uv`` call site overrides the single source with its own input."""
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
            if not isinstance(uses, str) or uses.split("@", 1)[0] != SETUP_UV_ACTION:
                continue
            inputs = step.get("with") or {}
            if not isinstance(inputs, dict):
                continue
            for name in OVERRIDING_INPUTS:
                if name in inputs:
                    findings.append(
                        Finding(
                            f"{path.relative_to(root)} ({step.get('name') or uses})",
                            f"setup-uv step sets '{name}: {inputs[name]}'. That input is "
                            "resolved AHEAD of the pyproject.toml scan, so this job would "
                            "silently use a different uv from every other job. Remove it and "
                            "let [tool.uv] required-version apply",
                        )
                    )
    return findings


def check_repo(root: Path) -> list[Finding]:
    """Run all three assertions against ``root``."""
    return check_pyproject(root) + check_no_root_uv_toml(root) + check_workflows(root)


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
        '[tool.uv] required-version = "==X.Y.Z" in pyproject.toml. Anything else puts a remote '
        "manifest fetch back on the critical path of every CI job (bug 56b7-b21a-c8ab-4afc).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
