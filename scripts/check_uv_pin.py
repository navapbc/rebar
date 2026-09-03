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
7. **Ignored by the container builds** — a Dockerfile that pulls uv from a tag this gate
   cannot read as the exact pin installs whatever upstream published most recently, which uv
   then rejects against ``required-version``. That covers a FLOATING tag
   (``ghcr.io/astral-sh/uv:latest``, or no tag at all), a build-ARG template
   (``uv:${UV_VERSION}``) that resolves only at build time, and a bare ``@sha256:`` digest —
   immutable, but OPAQUE: it never says which uv version it holds, and agreement with
   ``required-version``, not immutability, is the property under test, so for uv a digest is
   accepted only alongside a matching tag. This is the shape that took production down
   [rebar:febd-6b13-1976-43be]: CI honoured the pin while all three images did not, so every
   build died with "Required uv version ``==0.12.7`` does not match the running version
   ``0.12.9``" the moment ``:latest`` moved — a time bomb armed by an upstream release, with
   no change to this repository. The images must therefore name the EXACT
   ``required-version`` as their tag, and no Dockerfile may resolve any image from
   ``:latest``.

Stdlib + PyYAML only, with no CI provider required: it runs from ``make lint`` on a developer
laptop exactly as it runs in CI, which is the portability contract every gate here holds to.
"""

from __future__ import annotations

import argparse
import re
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

#: The uv distribution image the container builds copy the binary out of. Its tag is the
#: image-side half of the single source and must equal [tool.uv] required-version exactly.
UV_IMAGE_REPOSITORY = "ghcr.io/astral-sh/uv"

#: Image references in a Dockerfile: the `FROM <ref>` base and the `COPY --from=<ref>` source.
#: BOTH alternatives skip leading flags. `FROM --platform=$BUILDPLATFORM python:latest` names
#: an image exactly as `FROM python:latest` does; matching only the first token after FROM
#: captured the flag instead of the reference and let the floating tag through unchecked.
DOCKERFILE_IMAGE_PATTERN = re.compile(
    r"^\s*(?:FROM\s+(?:--\S+\s+)*(?P<from>\S+)|COPY\s+(?:--\S+\s+)*--from=(?P<copy>\S+))",
    re.IGNORECASE,
)

#: A build ARG / environment interpolation anywhere in an image reference: `$TAG`, `${TAG}`.
TEMPLATED_REFERENCE_PATTERN = re.compile(r"\$\{?[A-Za-z_]")
POWERSHELL_SCOPED_VARIABLE_PATTERN = re.compile(r"\$(?P<name>[A-Za-z_][A-Za-z0-9_]*):")
POWERSHELL_SCOPES = frozenset(
    {
        "env",
        "global",
        "local",
        "private",
        "script",
        "using",
        "variable",
    }
)
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


def _check_powershell_colon_interpolation(text: str) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[str] = set()
    for match in POWERSHELL_SCOPED_VARIABLE_PATTERN.finditer(text):
        name = match.group("name")
        if name.lower() in POWERSHELL_SCOPES or name in seen:
            continue
        seen.add(name)
        findings.append(
            Finding(
                str(LOCAL_SETUP_UV_ACTION_FILE),
                f"contains ${name}:, which PowerShell parses as a scoped variable. Use "
                f"${{{name}}}: when a variable is immediately followed by a colon",
            )
        )
    return findings


def check_local_action(root: Path) -> list[Finding]:
    """Assert the committed local action preserves the manifest-free uv install contract."""
    document, text, findings = _load_action(root)
    findings.extend(_check_powershell_colon_interpolation(text))
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


def _required_version(root: Path) -> str | None:
    """Return the bare ``X.Y.Z`` from ``[tool.uv] required-version``, or None if unusable.

    ``check_pyproject`` already reports a missing or inexact pin, so this returns None
    silently rather than double-reporting the same defect from a second checker.
    """
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:  # pragma: no cover - check_pyproject reports this
        return None
    required = data.get("tool", {}).get("uv", {}).get("required-version")
    if not isinstance(required, str) or not _is_exact_specifier(required):
        return None
    return required.strip()[2:].strip()


def _split_reference(reference: str) -> tuple[str, str | None, str | None]:
    """Split a registry reference into ``(repository, tag, digest)``.

    Tag and digest are INDEPENDENT coordinates, not alternatives: ``img:1.2.3@sha256:...``
    carries both, ``img@sha256:...`` only a digest, ``img:1.2.3`` only a tag. A port in the
    registry host (``host:5000/img``) is not a tag, which is why only the final path segment
    is inspected for one.
    """
    remainder, _, digest = reference.partition("@")
    final_segment = remainder.rsplit("/", 1)[-1]
    name, colon, tag = final_segment.partition(":")
    if colon and name and tag:
        return remainder[: len(remainder) - len(tag) - 1], tag, digest or None
    return remainder, None, digest or None


def _is_registry_reference(reference: str) -> bool:
    """Distinguish a registry image from a local build stage in ``COPY --from=``.

    ``COPY --from=builder`` and ``COPY --from=0`` name earlier stages of the same build;
    they resolve locally and cannot float. Only a reference carrying a registry separator
    (``/``, ``:`` or ``@``) reaches out to a registry.

    A build ARG is NOT excluded here. ``COPY --from=$BUILDER`` carries no separator and is
    already excluded as a stage name, while ``ghcr.io/astral-sh/uv:${UV_VERSION}`` does carry
    one and must reach the uv check rather than being silently skipped; whether an
    unresolvable template is a finding is decided per image in ``_check_dockerfile_reference``.
    """
    return any(character in reference for character in "/:@")


def _dockerfiles(root: Path) -> list[Path]:
    skipped = {".git", ".venv", "node_modules", "__pycache__"}
    return sorted(
        path
        for path in root.glob("**/Dockerfile*")
        if path.is_file() and not skipped.intersection(path.relative_to(root).parts)
    )


def _check_uv_reference(
    location: str, reference: str, tag: str | None, digest: str | None, version: str | None
) -> Finding | None:
    """Assert the uv image names the EXACT ``[tool.uv] required-version`` as its tag.

    uv is held to a stricter rule than every other image because this gate exists to prove
    the two halves of ONE pin agree. A digest is immutable but OPAQUE: it does not say which
    uv version it holds, so ``ghcr.io/astral-sh/uv@sha256:...`` cannot be read against
    ``required-version`` at all. Immutability is not the property under test here, agreement
    is, so for uv a digest is accepted only ALONGSIDE a matching tag
    (``uv:0.12.7@sha256:...``); every other image may still pin by bare digest.
    """
    pin = version or "X.Y.Z"
    if tag is None:
        suffix = f"@{digest}" if digest else ""
        return Finding(
            location,
            f"'{reference}' pins uv by digest alone. A digest is immutable but OPAQUE — it "
            "does not say which uv version it holds, so it cannot be checked against "
            "[tool.uv] required-version, which is the agreement this gate exists to prove. "
            f"Keep the digest and name the version too: {UV_IMAGE_REPOSITORY}:{pin}{suffix}",
        )
    if version is not None and tag != version:
        return Finding(
            location,
            f"installs uv {tag}, but [tool.uv] required-version pins {version}. uv reads "
            "that key itself and refuses to run on a mismatch, so every build here fails. "
            f"Use {UV_IMAGE_REPOSITORY}:{version}",
        )
    return None


def _check_dockerfile_reference(
    location: str, reference: str, version: str | None
) -> Finding | None:
    """Assert one image reference is exactly pinned, and uv-pinned to ``version``.

    A tag templated from a build ARG (``uv:${UV_VERSION}``) resolves only at build time, so
    this gate cannot read it. For the uv image that is a FAILURE — an unprovable pin is not a
    pin, and an ARG defaulting to a floating tag is exactly the escape that took production
    down. For every other image the template is left alone: choosing base-image versions is
    not what this gate single-sources.
    """
    repository, tag, digest = _split_reference(reference)
    is_uv = repository == UV_IMAGE_REPOSITORY
    if TEMPLATED_REFERENCE_PATTERN.search(reference):
        if not is_uv:
            return None
        return Finding(
            location,
            f"'{reference}' templates the uv reference from a build ARG, so this gate cannot "
            "prove it matches [tool.uv] required-version — and an ARG that defaults to a "
            "floating tag is the same outage with an extra hop. Name the exact version: "
            f"{UV_IMAGE_REPOSITORY}:{version or 'X.Y.Z'}",
        )
    if tag is None and digest is None:
        return Finding(
            location,
            f"'{reference}' names no tag, so it resolves to :latest. Pin an exact tag",
        )
    if tag == "latest":
        return Finding(
            location,
            f"'{reference}' resolves from the FLOATING :latest tag, so the image changes "
            "when upstream publishes, with no change to this repository. Pin an exact tag",
        )
    if not is_uv:
        return None
    return _check_uv_reference(location, reference, tag, digest, version)


def check_dockerfiles(root: Path) -> list[Finding]:
    """Assert every Dockerfile image is exactly pinned, and uv matches ``required-version``."""
    version = _required_version(root)
    findings: list[Finding] = []
    for path in _dockerfiles(root):
        relative = path.relative_to(root)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = DOCKERFILE_IMAGE_PATTERN.match(line)
            if match is None:
                continue
            reference = match.group("from") or match.group("copy")
            if not _is_registry_reference(reference):
                continue
            finding = _check_dockerfile_reference(f"{relative}:{number}", reference, version)
            if finding is not None:
                findings.append(finding)
    return findings


def check_repo(root: Path) -> list[Finding]:
    """Run all uv-pin assertions against ``root``."""
    return (
        check_pyproject(root)
        + check_no_root_uv_toml(root)
        + check_local_action(root)
        + check_workflows(root)
        + check_dockerfiles(root)
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
        f"fetches. Every Dockerfile must name {UV_IMAGE_REPOSITORY} with that same exact "
        "version as its TAG (a digest may accompany the tag but never replace it, and a "
        "build-ARG template is not a provable pin), and no Dockerfile may resolve any image "
        "from a floating :latest tag.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
