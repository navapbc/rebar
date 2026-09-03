"""The uv-pin gate must FAIL on skew, not merely pass on the happy tree [rebar:56b7-b21a-c8ab-4afc].

A guard that can only ever pass has validated nothing. Each test here builds a minimal tree,
introduces exactly ONE way the single-sourced, local-action pin can be defeated, and asserts the
checker rejects it and names the offending location — so the gate's failing state is proven
rather than assumed. The happy-path test additionally runs against this repository's real root,
so the gate and the tree it governs cannot drift apart silently.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "check_uv_pin.py"

#: The one Dockerfile every skew test mutates, and the line its uv reference sits on. Skew
#: assertions bind to this exact `path:line` so they name the offender rather than matching
#: the always-emitted trailer summary, which mentions every failure mode by construction.
SVC_DOCKERFILE = Path("infra") / "compose" / "Dockerfile.svc"
UV_LINE = 3
DIGEST = "sha256:" + "0" * 64


def _gate_module() -> ModuleType:
    """Import the gate by path (``scripts/`` is not an importable package)."""
    spec = importlib.util.spec_from_file_location("check_uv_pin", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PYPROJECT_PINNED = """\
[project]
name = "fixture"
version = "0.1.0"

[tool.uv]
required-version = "==0.12.7"
"""

WORKFLOW_CLEAN = """\
name: fixture
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
      - uses: ./.github/actions/setup-uv
        with:
          enable-cache: true
"""

CHECKSUM_LINES = "\n".join(
    f"        {name}: {value}"
    for name, value in {
        "UV_SHA256_AARCH64_APPLE_DARWIN": (
            "127ebdda7ad953cdf198e964b570ea5771b85467ea93eb7cb6d6f8e6f55408f3"
        ),
        "UV_SHA256_AARCH64_PC_WINDOWS_MSVC": (
            "1611d0f4be72b0a354ad9a6ae954093dd4c91e93e36b8b490326a05a039ffe14"
        ),
        "UV_SHA256_AARCH64_UNKNOWN_LINUX_GNU": (
            "66393193038dd7eb108abd7a218d9cec04ac70ab98242b0720fa94de19223b7c"
        ),
        "UV_SHA256_X86_64_APPLE_DARWIN": (
            "06b8ae1da8c2661c5434507a66f8c2b0b835933bf955b5958a9ac357a37d1959"
        ),
        "UV_SHA256_X86_64_PC_WINDOWS_MSVC": (
            "bf1518af459a3915511a11fdc6e2f43ef9a2afa138b9d498eeb9642fe9d85218"
        ),
        "UV_SHA256_X86_64_UNKNOWN_LINUX_GNU": (
            "788f18abea7c5f55d6216e4f5613fd89d4d59b631efeec117b2b07fe72f1da21"
        ),
    }.items()
)

ACTION_CLEAN = f"""\
name: Setup uv
description: Install the repository-pinned uv release without fetching the setup-uv manifest.
inputs:
  enable-cache:
    description: Restore and save uv's cache directory.
    default: "false"
runs:
  using: composite
  steps:
    - name: Install uv
      shell: pwsh
      env:
{CHECKSUM_LINES}
      run: |
        echo "placeholder"
    - name: Cache uv downloads and wheels
      if: inputs.enable-cache == 'true'
      uses: actions/cache@v6
      with:
        path: ~/.cache/uv
        key: fixture
"""


DOCKERFILE_CLEAN = """\
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /usr/local/bin/uv
RUN uv sync --locked
"""


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the checker as a subprocess against ``root``, exactly as ``make lint`` does."""
    return subprocess.run(
        [sys.executable, str(GATE), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal, correctly pinned repository: the baseline every skew mutates."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT_PINNED, encoding="utf-8")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "build.yml").write_text(WORKFLOW_CLEAN, encoding="utf-8")
    action = tmp_path / ".github" / "actions" / "setup-uv"
    action.mkdir(parents=True)
    (action / "action.yml").write_text(ACTION_CLEAN, encoding="utf-8")
    compose = tmp_path / "infra" / "compose"
    compose.mkdir(parents=True)
    (compose / "Dockerfile.svc").write_text(DOCKERFILE_CLEAN, encoding="utf-8")
    return tmp_path


def test_real_repository_passes() -> None:
    """The gate is green on the tree it actually governs."""
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stderr


def test_fixture_baseline_passes(tree: Path) -> None:
    """The baseline must pass, or a skew test could pass for the wrong reason."""
    result = _run(tree)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("specifier", ['">=0.12.7"', '"~=0.12.0"', '"0.12.7"', '"==0.12.*"'])
def test_range_specifier_is_rejected(tree: Path, specifier: str) -> None:
    """A range still triggers the manifest fetch, so it must not read as pinned.

    `"0.12.7"` (no operator) is included deliberately: uv accepts it, so it looks correct, but
    the action's `normalizeVersionSpecifier` only strips a leading `==` — this gate insists on
    the form that is unambiguously exact to BOTH readers.
    """
    (tree / "pyproject.toml").write_text(
        PYPROJECT_PINNED.replace('"==0.12.7"', specifier), encoding="utf-8"
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "exact" in result.stderr


def test_missing_pin_is_rejected(tree: Path) -> None:
    """Deleting the section restores the original outage, so it must fail loudly."""
    (tree / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "required-version" in result.stderr


def test_root_uv_toml_is_rejected(tree: Path) -> None:
    """A root uv.toml shadows the pin for setup-uv AND for uv itself."""
    (tree / "uv.toml").write_text('required-version = "==0.11.0"\n', encoding="utf-8")
    result = _run(tree)
    assert result.returncode == 1
    assert "uv.toml" in result.stderr


def test_astral_setup_uv_call_site_is_rejected(tree: Path) -> None:
    """The local action replaces setup-uv so the manifest-fetching action cannot return."""
    workflow = WORKFLOW_CLEAN.replace("./.github/actions/setup-uv", "astral-sh/setup-uv@v10")
    (tree / ".github" / "workflows" / "build.yml").write_text(workflow, encoding="utf-8")
    result = _run(tree)
    assert result.returncode == 1
    assert "astral-sh/setup-uv" in result.stderr


def test_local_setup_uv_version_override_is_rejected(tree: Path) -> None:
    """A local call site must not defeat pyproject's single-sourced version."""
    workflow = WORKFLOW_CLEAN.replace(
        "          enable-cache: true\n",
        "          enable-cache: true\n          version: 0.12.6\n",
    )
    (tree / ".github" / "workflows" / "build.yml").write_text(workflow, encoding="utf-8")
    result = _run(tree)
    assert result.returncode == 1
    assert "version" in result.stderr


def test_local_setup_uv_version_file_override_is_rejected(tree: Path) -> None:
    """`version-file:` is the sibling override and must be rejected the same way."""
    workflow = WORKFLOW_CLEAN.replace(
        "          enable-cache: true\n",
        "          enable-cache: true\n          version-file: uv.toml\n",
    )
    (tree / ".github" / "workflows" / "build.yml").write_text(workflow, encoding="utf-8")
    result = _run(tree)
    assert result.returncode == 1
    assert "version-file" in result.stderr


def test_missing_local_action_is_rejected(tree: Path) -> None:
    """The guard must fail if workflows point at an action that is not committed."""
    (tree / ".github" / "actions" / "setup-uv" / "action.yml").unlink()
    result = _run(tree)
    assert result.returncode == 1
    assert ".github/actions/setup-uv/action.yml" in result.stderr


def test_manifest_source_in_local_action_is_rejected(tree: Path) -> None:
    """The replacement action must not regain setup-uv's raw manifest fetch."""
    action = tree / ".github" / "actions" / "setup-uv" / "action.yml"
    action.write_text(action.read_text(encoding="utf-8") + "\n# raw.githubusercontent.com\n")
    result = _run(tree)
    assert result.returncode == 1
    assert "raw.githubusercontent.com" in result.stderr


def test_powershell_colon_interpolation_in_local_action_is_rejected(tree: Path) -> None:
    """PowerShell parses `$name:` in strings as a scoped variable, failing before install."""
    action = tree / ".github" / "actions" / "setup-uv" / "action.yml"
    action.write_text(
        action.read_text(encoding="utf-8").replace(
            '        echo "placeholder"\n',
            '        throw "Checksum mismatch for $asset: bad"\n',
        ),
        encoding="utf-8",
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "$asset:" in result.stderr


def test_legitimate_powershell_scoped_variables_in_local_action_are_allowed(tree: Path) -> None:
    """The colon guard must not reject valid PowerShell scopes such as `$env:`."""
    action = tree / ".github" / "actions" / "setup-uv" / "action.yml"
    action.write_text(
        action.read_text(encoding="utf-8").replace(
            '        echo "placeholder"\n',
            '        Write-Host "runner is $env:RUNNER_OS"\n'
            '        Write-Host "path file is $env:GITHUB_PATH"\n',
        ),
        encoding="utf-8",
    )
    result = _run(tree)
    assert result.returncode == 0, result.stderr


def test_local_action_missing_checksum_is_rejected(tree: Path) -> None:
    """Every supported runner target needs a committed digest before CI can trust it."""
    action = tree / ".github" / "actions" / "setup-uv" / "action.yml"
    checksum_line = (
        "        UV_SHA256_X86_64_PC_WINDOWS_MSVC: "
        "bf1518af459a3915511a11fdc6e2f43ef9a2afa138b9d498eeb9642fe9d85218\n"
    )
    action.write_text(
        action.read_text(encoding="utf-8").replace(checksum_line, ""),
        encoding="utf-8",
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "UV_SHA256_X86_64_PC_WINDOWS_MSVC" in result.stderr


def test_checksum_mismatch_in_local_action_is_rejected(tree: Path) -> None:
    """The committed checksums are part of the pinned artifact contract."""
    action = tree / ".github" / "actions" / "setup-uv" / "action.yml"
    action.write_text(
        action.read_text(encoding="utf-8").replace(
            "788f18abea7c5f55d6216e4f5613fd89d4d59b631efeec117b2b07fe72f1da21",
            "0" * 64,
        ),
        encoding="utf-8",
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "UV_SHA256_X86_64_UNKNOWN_LINUX_GNU" in result.stderr


def test_local_action_emits_acceptance_anchor() -> None:
    """CI greps this exact install liveness line before checking manifest absence."""
    body = (REPO_ROOT / ".github" / "actions" / "setup-uv" / "action.yml").read_text(
        encoding="utf-8"
    )
    assert "Successfully installed uv $version" in body


def test_make_lint_invokes_the_gate() -> None:
    """A gate nothing runs is not a gate.

    `make lint` is the portable, no-CI-provider trigger the project uses for every checker of
    this kind, and CI inherits it through the same step -- so wiring is part of the contract,
    not an implementation detail.
    """
    body = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "python scripts/check_uv_pin.py" in body


def test_real_repository_dockerfiles_do_not_float_the_uv_tag() -> None:
    """AC1/AC3 -- no container build may resolve uv from a moving upstream tag.

    ``:latest`` moved to 0.12.9 while ``[tool.uv] required-version`` stayed at 0.12.7, and
    ``uv sync`` reads that key itself: every image build died with "Required uv version
    `==0.12.7` does not match the running version `0.12.9`". The defect is a TIME BOMB by
    construction -- it fires when upstream publishes, with no change to this repository -- so
    the tree itself, not only a fixture, must be asserted free of floating toolchain tags.
    """
    offenders = [
        f"{path}:{number}"
        for path in _gate_module()._dockerfiles(REPO_ROOT)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if ":latest" in line and line.lstrip().startswith(("FROM", "COPY --from="))
    ]
    assert not offenders, f"floating :latest toolchain tags: {offenders}"


def test_real_repository_dockerfiles_match_the_pyproject_pin() -> None:
    """AC2 -- the images must install the uv that ``pyproject.toml`` requires."""
    required = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = required["tool"]["uv"]["required-version"].removeprefix("==")
    for path in sorted(REPO_ROOT.glob("infra/compose/Dockerfile.*")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "astral-sh/uv" not in line:
                continue
            assert f"astral-sh/uv:{version}" in line, f"{path.name}: {line.strip()}"


def test_dockerfile_floating_uv_tag_is_rejected(tree: Path) -> None:
    """The exact production defect: ``uv:latest`` against an exact ``required-version``."""
    dockerfile = tree / "infra" / "compose" / "Dockerfile.svc"
    dockerfile.write_text(DOCKERFILE_CLEAN.replace("uv:0.12.7", "uv:latest"), encoding="utf-8")
    result = _run(tree)
    assert result.returncode == 1
    assert (
        f"{SVC_DOCKERFILE}:{UV_LINE}: 'ghcr.io/astral-sh/uv:latest' resolves from the "
        "FLOATING :latest tag"
    ) in result.stderr


def test_dockerfile_uv_tag_skewed_from_pyproject_is_rejected(tree: Path) -> None:
    """A pinned-but-WRONG tag is the same outage with a slower fuse."""
    dockerfile = tree / "infra" / "compose" / "Dockerfile.svc"
    dockerfile.write_text(DOCKERFILE_CLEAN.replace("uv:0.12.7", "uv:0.12.9"), encoding="utf-8")
    result = _run(tree)
    assert result.returncode == 1
    assert "0.12.9" in result.stderr
    assert "0.12.7" in result.stderr


def test_dockerfile_untagged_uv_reference_is_rejected(tree: Path) -> None:
    """An omitted tag IS ``:latest`` -- the identical failure, spelled differently."""
    dockerfile = tree / "infra" / "compose" / "Dockerfile.svc"
    dockerfile.write_text(DOCKERFILE_CLEAN.replace("uv:0.12.7", "uv"), encoding="utf-8")
    result = _run(tree)
    assert result.returncode == 1
    assert (f"{SVC_DOCKERFILE}:{UV_LINE}: 'ghcr.io/astral-sh/uv' names no tag") in result.stderr


def test_dockerfile_floating_base_image_is_rejected(tree: Path) -> None:
    """AC3 generalises beyond uv: no Dockerfile may pull ANY image from ``:latest``."""
    dockerfile = tree / "infra" / "compose" / "Dockerfile.svc"
    dockerfile.write_text(
        DOCKERFILE_CLEAN.replace("python:3.12-slim", "python:latest"), encoding="utf-8"
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "python:latest" in result.stderr


def test_dockerfile_named_build_stage_reference_is_allowed(tree: Path) -> None:
    """``COPY --from=builder`` names a local stage, not a registry image -- never a finding."""
    dockerfile = tree / "infra" / "compose" / "Dockerfile.svc"
    dockerfile.write_text(
        "FROM python:3.12-slim AS builder\n"
        "FROM python:3.12-slim\n"
        "COPY --from=builder /app /app\n"
        "COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /usr/local/bin/uv\n",
        encoding="utf-8",
    )
    result = _run(tree)
    assert result.returncode == 0, result.stderr


def test_dockerfile_uv_digest_without_a_tag_is_rejected(tree: Path) -> None:
    """A digest is immutable but OPAQUE, so for uv it is not a pin this gate can read.

    This is the honest reading of what the gate is FOR. It does not exist to make the image
    reproducible -- a digest already does that -- it exists to prove the image half and the
    ``[tool.uv] required-version`` half of ONE pin agree. ``uv@sha256:...`` says nothing about
    which uv version it holds, so it cannot be checked against ``required-version`` at all,
    and accepting it would silently reopen the exact skew that took production down.
    """
    dockerfile = tree / "infra" / "compose" / "Dockerfile.svc"
    dockerfile.write_text(DOCKERFILE_CLEAN.replace("uv:0.12.7", f"uv@{DIGEST}"), encoding="utf-8")
    result = _run(tree)
    assert result.returncode == 1
    assert f"{SVC_DOCKERFILE}:{UV_LINE}: " in result.stderr
    assert "pins uv by digest alone" in result.stderr
    assert f"ghcr.io/astral-sh/uv:0.12.7@{DIGEST}" in result.stderr


def test_dockerfile_uv_tag_with_matching_digest_is_allowed(tree: Path) -> None:
    """A digest ALONGSIDE the exact tag is the strongest form, so it must stay allowed.

    Tag and digest are independent coordinates: the tag carries the version this gate checks,
    the digest carries immutability. Requiring the tag must not punish also pinning the bytes.
    """
    dockerfile = tree / "infra" / "compose" / "Dockerfile.svc"
    dockerfile.write_text(
        DOCKERFILE_CLEAN.replace("uv:0.12.7", f"uv:0.12.7@{DIGEST}"), encoding="utf-8"
    )
    result = _run(tree)
    assert result.returncode == 0, result.stderr


def test_dockerfile_uv_skewed_tag_with_digest_is_still_rejected(tree: Path) -> None:
    """A digest does not launder a tag that disagrees with ``required-version``."""
    dockerfile = tree / "infra" / "compose" / "Dockerfile.svc"
    dockerfile.write_text(
        DOCKERFILE_CLEAN.replace("uv:0.12.7", f"uv:0.12.9@{DIGEST}"), encoding="utf-8"
    )
    result = _run(tree)
    assert result.returncode == 1
    assert f"{SVC_DOCKERFILE}:{UV_LINE}: installs uv 0.12.9" in result.stderr


def test_dockerfile_non_uv_digest_without_a_tag_is_allowed(tree: Path) -> None:
    """For any OTHER image a bare digest is an exact pin, and the real tree relies on it.

    ``tests/external/live_jira_dc/Dockerfile`` pins its base image by digest alone; the uv
    rule is deliberately narrower than "every image needs a tag".
    """
    dockerfile = tree / "infra" / "compose" / "Dockerfile.svc"
    dockerfile.write_text(
        DOCKERFILE_CLEAN.replace("python:3.12-slim", f"python@{DIGEST}"), encoding="utf-8"
    )
    result = _run(tree)
    assert result.returncode == 0, result.stderr


def test_dockerfile_platform_flag_does_not_hide_a_floating_tag(tree: Path) -> None:
    """``FROM --platform=...`` names an image exactly as a bare ``FROM`` does.

    Matching only the first token after ``FROM`` captured the FLAG, so the reference behind it
    was never inspected and any floating tag walked straight through the gate.
    """
    dockerfile = tree / "infra" / "compose" / "Dockerfile.svc"
    dockerfile.write_text(
        DOCKERFILE_CLEAN.replace(
            "FROM python:3.12-slim", "FROM --platform=$BUILDPLATFORM python:latest"
        ),
        encoding="utf-8",
    )
    result = _run(tree)
    assert result.returncode == 1
    assert f"{SVC_DOCKERFILE}:1: 'python:latest' resolves from the FLOATING" in result.stderr


def test_dockerfile_platform_flag_on_a_pinned_image_is_allowed(tree: Path) -> None:
    """Skipping the flag must find the reference, not merely fail on flagged lines."""
    dockerfile = tree / "infra" / "compose" / "Dockerfile.svc"
    dockerfile.write_text(
        DOCKERFILE_CLEAN.replace(
            "FROM python:3.12-slim", "FROM --platform=$BUILDPLATFORM python:3.12-slim"
        ),
        encoding="utf-8",
    )
    result = _run(tree)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("template", ["${UV_VERSION}", "$UV_VERSION"])
def test_dockerfile_arg_templated_uv_tag_is_rejected(tree: Path, template: str) -> None:
    """An ARG-templated uv tag is UNPROVABLE, so it must fail rather than be skipped.

    ``ARG UV_VERSION=latest`` + ``COPY --from=ghcr.io/astral-sh/uv:${UV_VERSION}`` is the
    production outage with one extra hop: the tag resolves only at build time, so the gate
    cannot show it equals ``required-version``. Treating it as "not a registry reference" --
    which is what the braced form used to do -- deleted the check entirely.
    """
    dockerfile = tree / "infra" / "compose" / "Dockerfile.svc"
    dockerfile.write_text(DOCKERFILE_CLEAN.replace("uv:0.12.7", f"uv:{template}"), encoding="utf-8")
    result = _run(tree)
    assert result.returncode == 1
    assert f"{SVC_DOCKERFILE}:{UV_LINE}: " in result.stderr
    assert "templates the uv reference from a build ARG" in result.stderr


def test_dockerfile_arg_templated_non_uv_tag_is_allowed(tree: Path) -> None:
    """A templated BASE-image tag is left alone: this gate single-sources uv, not Python.

    Failing every ``$TAG`` would be a base-image policy change, far wider than the pin this
    gate owns; the uv image is the one whose version this repository already asserts.
    """
    dockerfile = tree / "infra" / "compose" / "Dockerfile.svc"
    dockerfile.write_text(
        DOCKERFILE_CLEAN.replace("python:3.12-slim", "python:$PY_TAG"), encoding="utf-8"
    )
    result = _run(tree)
    assert result.returncode == 0, result.stderr


def test_dockerfile_templated_build_stage_reference_is_allowed(tree: Path) -> None:
    """``COPY --from=$BUILDER`` still names a local stage, not a registry image."""
    dockerfile = tree / "infra" / "compose" / "Dockerfile.svc"
    dockerfile.write_text(
        "ARG BUILDER=builder\n"
        "FROM python:3.12-slim AS builder\n"
        "FROM python:3.12-slim\n"
        "COPY --from=$BUILDER /app /app\n"
        "COPY --from=${BUILDER} /app /app2\n"
        "COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /usr/local/bin/uv\n",
        encoding="utf-8",
    )
    result = _run(tree)
    assert result.returncode == 0, result.stderr
