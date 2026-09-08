"""Packaging dependency contracts.

Keep ``inspect-ai``, its empty ``eval`` extra, related resolver conflicts, and its
Click override removed. Every name in :data:`rebar._optional.EXTRAS` must map to a
declared project optional-dependency key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib
from packaging.requirements import InvalidRequirement, Requirement

from rebar import _optional

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


@pytest.fixture(scope="module")
def pyproject() -> dict:
    if not _PYPROJECT.is_file():
        pytest.skip("pyproject.toml not present (installed-package test run)")
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def _all_requirements(pyproject: dict) -> list[str]:
    project = pyproject.get("project", {})
    reqs = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        reqs.extend(group)
    return reqs


def _direct_requirement(pyproject: dict, name: str) -> Requirement | None:
    """Return the parsed DIRECT requirement named ``name`` (e.g. ``anthropic``), or None.

    A ``pydantic-ai-slim[anthropic]`` entry declares the extra ``anthropic`` but its own
    distribution name is ``pydantic-ai-slim``, so it never matches — only a first-class
    ``anthropic ...`` requirement line does.
    """
    for raw in _all_requirements(pyproject):
        try:
            parsed = Requirement(raw)
        except InvalidRequirement:
            continue
        if parsed.name == name:
            return parsed
    return None


def test_anthropic_sdk_direct_floor_allows_the_httpx2_line(pyproject) -> None:
    """Keep an SDK floor for lowest-direct while allowing the supported httpx2 line."""
    anthropic = _direct_requirement(pyproject, "anthropic")
    assert anthropic is not None, (
        "the anthropic SDK must remain a DIRECT dependency of the [agents] extra so the "
        "lowest-direct sweep leg keeps a known-good floor instead of falling below pydantic-ai's "
        "tested SDK window"
    )
    spec = anthropic.specifier
    assert spec.contains("0.121.0", prereleases=True), (
        f"anthropic bound {spec} excludes 0.121.0, the pre-httpx2 SDK the provider seam "
        "must keep supporting"
    )
    assert spec.contains("1.2.0", prereleases=True), (
        f"anthropic bound {spec} still excludes the httpx2 SDK line that 2bd6 supports; "
        "remove or widen the 1f35 temporary cap"
    )
    assert not spec.contains("2.0.0", prereleases=True), (
        f"anthropic bound {spec} admits an unreviewed next major; keep a ceiling while widening "
        "the 1f35 cap enough for the supported httpx2 line"
    )


def test_inspect_ai_is_not_a_dependency(pyproject) -> None:
    """inspect-ai was never imported by rebar; it must not come back as a dependency."""
    offenders = [r for r in _all_requirements(pyproject) if "inspect-ai" in r or "inspect_ai" in r]
    assert offenders == [], f"inspect-ai reintroduced as a dependency: {offenders}"


def test_eval_extra_is_gone(pyproject) -> None:
    """`[eval]` held inspect-ai and nothing else, so it was removed rather than emptied."""
    extras = pyproject["project"].get("optional-dependencies", {})
    assert "eval" not in extras, (
        "the [eval] extra is back — the offline prompt-eval surface needs no extra and the "
        "live run needs [agents]; a second name for [agents] is not an extra"
    )


def test_no_dependency_overrides(pyproject) -> None:
    """Keep overrides exceptional. Normal resolution now selects a safe Click version."""
    assert "override-dependencies" not in pyproject.get("tool", {}).get("uv", {})


def test_no_resolution_conflict_mentions_the_eval_extra(pyproject) -> None:
    """The eval-vs-dev / eval-vs-bedrock forks existed only for inspect-ai's boto3 diamond."""
    conflicts = pyproject.get("tool", {}).get("uv", {}).get("conflicts", [])
    named = [c for c in conflicts if any(item.get("extra") == "eval" for item in c)]
    assert named == [], f"a resolution conflict still names the removed [eval] extra: {named}"


def test_every_registered_extra_exists_in_pyproject(pyproject) -> None:
    """`_optional.EXTRAS` drives a user-facing `pip install 'nava-rebar[<extra>]'` hint.

    An entry naming an extra that pyproject does not declare sends users at an install that
    silently installs nothing — exactly the drift that outlived inspect-ai's removal here.
    """
    declared = set(pyproject["project"].get("optional-dependencies", {}))
    missing = sorted(set(_optional.EXTRAS) - declared)
    assert missing == [], f"EXTRAS names extras that pyproject does not declare: {missing}"
