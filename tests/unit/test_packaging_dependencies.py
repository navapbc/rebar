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


def test_the_research_corpus_is_excluded_from_both_distributions() -> None:
    """``evals/`` is research, not product, and must not ship (epic e47b).

    The sdist's ``include`` allowlist now keeps it out on its own: the README entry is
    root-anchored (``"/README.md"``), so it is no longer the depth-agnostic glob that
    admitted any directory holding a ``README.md`` — ``docs/experiments/`` before the
    corpus moved, ``evals/`` after (bug 9cf1-3663-4de6-4844). The explicit ``exclude``
    entries remain as belt-and-suspenders, and this guards them so a future ``include``
    edit cannot quietly start shipping the tree again.
    """
    build = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["tool"]["hatch"]["build"][
        "targets"
    ]

    assert "evals" not in build["wheel"]["packages"]
    sdist_exclude = build["sdist"]["exclude"]
    for pattern in ("/evals", "evals", "evals/**"):
        assert pattern in sdist_exclude, (
            f"{pattern!r} missing from the sdist exclude list; the include allowlist's "
            "depth-agnostic README.md glob would otherwise ship research READMEs"
        )


# What the sdist archive carries at its top level, and why each entry is there.
# `src`, `tests` and `README.md` are the `include` allowlist. The rest are hatchling's
# own additions, each traceable to a declaration in pyproject.toml rather than to a
# pattern: PKG-INFO and pyproject.toml are sdist metadata, LICENSE and docs/ come from
# PEP 639 `license-files`, hatch_build.py is the `[tool.hatch.build.hooks.custom]` hook,
# and .gitignore is hatchling's default sdist inclusion.
_SDIST_ALLOWLIST_ENTRIES = frozenset({"src", "tests", "README.md"})
_SDIST_DECLARED_EXTRAS = frozenset(
    {"PKG-INFO", "pyproject.toml", "LICENSE", "docs", "hatch_build.py", ".gitignore"}
)


def _built_top_level(builder, tmp_path) -> tuple[set[str], list[str]]:
    """Build one distribution in-process and return its top-level entries and all paths.

    Builds the REAL archive (~5 s) rather than inspecting patterns, because the defect
    this guards was invisible to pattern reading: a bare ``README.md`` looks anchored and
    is not.
    """
    import tarfile
    import zipfile

    artifact = next(iter(builder.build(directory=str(tmp_path), versions=["standard"])))
    if artifact.endswith(".whl"):
        with zipfile.ZipFile(artifact) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
        return {n.split("/")[0] for n in names}, names
    with tarfile.open(artifact) as archive:
        names = [m.name for m in archive.getmembers() if m.isfile()]
    root = names[0].split("/")[0]
    stripped = [n[len(root) + 1 :] for n in names]
    return {n.split("/")[0] for n in stripped}, stripped


def test_the_sdist_ships_only_its_allowlist_and_declared_metadata(tmp_path) -> None:
    """The sdist's top-level entry set is exactly the allowlist plus declared metadata.

    Bug 9cf1-3663-4de6-4844: ``include = [..., "README.md"]`` reads as an allowlist but
    the bare entry is a depth-agnostic glob in hatchling, so every ``README.md`` in the
    tree matched and dragged its parent directory into the sdist — measured on this tree
    as ``.github/``, ``docs/``, ``examples/`` and ``scripts/``. Anchoring the entry to
    ``/README.md`` makes the allowlist mean what it says, and building the archive here
    is what catches a future unanchored pattern; reading the pattern never did.
    """
    pytest.importorskip("hatchling")
    from hatchling.builders.sdist import SdistBuilder

    tops, paths = _built_top_level(SdistBuilder(str(_PYPROJECT.parent)), tmp_path)

    assert tops == _SDIST_ALLOWLIST_ENTRIES | _SDIST_DECLARED_EXTRAS, (
        "the sdist's top-level entries drifted from the declared set; a new entry must "
        "either be named in the `include` allowlist or be a declared metadata addition "
        "recorded in _SDIST_DECLARED_EXTRAS with its reason"
    )
    stray = [
        p
        for p in paths
        if p.endswith("README.md") and p != "README.md" and not p.startswith("tests/")
    ]
    assert stray == [], f"README.md files outside the declared set shipped: {stray}"


def test_the_wheel_ships_only_the_package_and_its_metadata(tmp_path) -> None:
    """The wheel carries the ``rebar`` package and its dist-info, nothing else.

    The wheel was believed unaffected by the sdist's glob because it builds from
    ``packages = ["src/rebar"]``. This measures that belief instead of assuming it.
    """
    pytest.importorskip("hatchling")
    from hatchling.builders.wheel import WheelBuilder

    tops, paths = _built_top_level(WheelBuilder(str(_PYPROJECT.parent)), tmp_path)

    dist_info = {t for t in tops if t.endswith(".dist-info")}
    assert len(dist_info) == 1, f"expected exactly one .dist-info, got {sorted(dist_info)}"
    assert tops - dist_info == {"rebar"}, (
        f"the wheel shipped unexpected top-level entries: {sorted(tops - dist_info - {'rebar'})}"
    )
    assert [p for p in paths if p.endswith("README.md")] == []
