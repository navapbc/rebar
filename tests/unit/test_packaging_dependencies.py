"""Packaging-contract tests for the dependency tree declared in ``pyproject.toml``.

These pin removals that are easy to silently reintroduce and expensive when they come
back (ticket 9fb3): the unused ``inspect-ai`` dependency, the ``[eval]`` extra it was the
sole content of, the two ``[tool.uv] conflicts`` entries that forked the resolution around
its boto3 diamond, and the ``override-dependencies`` entry that existed only to widen the
stale ``click`` cap it carried.

They also check the inverse drift: every extra named in :data:`rebar._optional.EXTRAS` must
resolve to a real key in ``[project.optional-dependencies]``, so the guard can never again
advertise ``pip install 'nava-rebar[<extra>]'`` for an extra that does not exist.
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


def test_anthropic_sdk_is_bounded_below_the_httpx2_line(pyproject) -> None:
    """The direct `anthropic` SDK must carry a compatible RANGE excluding the httpx2 line.

    `pydantic-ai-slim[anthropic]` pulls the anthropic SDK but caps only pydantic-ai
    (`>=1.107,<2`), leaving the SDK itself unbounded (bug 1f35). anthropic 1.0.0 switched
    its client to a vendored `httpx2` and REJECTS a stdlib `httpx.AsyncClient`, so the
    provider seam (`_build_retrying_anthropic_provider`, which passes `httpx.AsyncClient`)
    raises `TypeError: Invalid http_client argument; Expected ... httpx2.AsyncClient`.
    `uv.lock` masks this on the merge gate, but the unlocked mirror-sweep legs float the SDK
    to the latest (httpx2) release and fail ~90 tests.

    The declared bound must therefore be a DIRECT dependency with a real floor AND a ceiling:
    the ceiling excludes the httpx2 releases on a highest-version resolve, and the floor keeps
    the `--resolution lowest-direct` sweep leg (which pins a direct constraint to its floor)
    from dropping onto an ancient, incompatible SDK. anthropic `0.x` (through 0.125.0) declares
    stdlib `httpx<1`; `1.0.0`/`1.1.0`/`1.2.0` declare `httpx2>=2` — so the boundary is `<1`.
    """
    anthropic = _direct_requirement(pyproject, "anthropic")
    assert anthropic is not None, (
        "the anthropic SDK must be a DIRECT, bounded dependency of the [agents] extra — "
        "leaving it to float via `pydantic-ai-slim[anthropic]` lets the mirror sweep resolve "
        "the httpx2-vendoring release that breaks the provider seam (bug 1f35)"
    )
    spec = anthropic.specifier
    # The compatible, lockfile-verified SDK (stdlib httpx) must remain installable — this is
    # what uv.lock resolves and the provider seam is verified against, and it is the floor the
    # `--resolution lowest-direct` sweep leg lands on.
    assert spec.contains("0.121.0", prereleases=True), (
        f"anthropic bound {spec} excludes 0.121.0, the version uv.lock resolves and the "
        "provider seam is verified against — the range needs a floor no higher than it"
    )
    # ...and every httpx2-vendoring release (first at 1.0.0, latest 1.2.0) must be excluded,
    # so no sweep resolution can float the SDK onto the client the provider seam rejects.
    for httpx2_release in ("1.0.0", "1.1.0", "1.2.0"):
        assert not spec.contains(httpx2_release, prereleases=True), (
            f"anthropic bound {spec} still admits {httpx2_release}, which vendors httpx2 and "
            "rejects the stdlib httpx.AsyncClient passed by the provider seam (bug 1f35)"
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
    """`override-dependencies` forces an untested combination and must stay a last resort.

    The only one rebar ever carried (`click>=8.3.3`) existed to widen inspect-ai's stale cap;
    with inspect-ai gone the resolver reaches a non-vulnerable click on its own. A new
    override needs the written justification the dependency-advisory runbook demands, and
    updating this test is the deliberate step that forces it.
    """
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
