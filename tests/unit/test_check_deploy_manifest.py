"""Self-tests for the deploy-manifest completeness gate (ticket ``0a6a-04d3-8fd9-4cd5``).

The gate exists because ``infra/scripts/autodeploy.sh``'s hand-curated ``*_PATHS`` manifests
have silently fallen out of sync with reality four times — a deploy-relevant infra file added
but never listed, so a later change to it drifts to the running box with no signal. The gate
DERIVES the expected path set from Dockerfile/compose directives and filename conventions and
fails on drift, so the enforced list can never silently diverge.

These tests pin, against the REAL repo:
  * the guard PASSES on the complete manifests (current ``origin/main``);
  * a RED case — a derived path omitted from every manifest fails for the RIGHT reason,
    naming that path and the derivation source that matched it;
  * the mutation check — re-omitting any covered derived path returns the gate to RED, so the
    coverage check has teeth for each manifest entry, then restore;
  * the exclusion mechanism (each entry has a reason, and an excluded path is suppressed);
  * the gate's wiring into ``make lint`` (a CI-only gate lets a local verdict be green over a
    tree CI rejects).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_deploy_manifest.py"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import check_deploy_manifest as gate  # noqa: E402

# The recurrence classes the derivation must catch, each covered on origin/main.
_COVERED_DERIVED = [
    "infra/scripts/mcp-entrypoint.sh",  # 5d4c — Dockerfile.mcp install/ENTRYPOINT + glob
    "infra/gerrit/materialize-deploy-key.sh",  # 408c — materialize-*.sh glob
    "infra/scripts/materialize-mcp-upstream.sh",  # 5524 — materialize-*.sh glob
    "infra/scripts/compose-up.sh",  # compose-up.sh glob
    "infra/scripts/reviewbot-ensure-tickets.sh",  # Dockerfile.reviewbot install
]


def _real_manifest_text() -> str:
    return gate._autodeploy_path(_REPO_ROOT).read_text(encoding="utf-8")


# ─────────────────────────── the gate passes on the complete tree ───────────────────────────


def test_passes_against_real_repo() -> None:
    """With the manifests complete on origin/main the gate is clean."""
    assert gate.check(_REPO_ROOT) == []
    assert gate.main([]) == 0


def test_derivation_covers_every_recurrence_class() -> None:
    """The derivation names each historically-drifted path, with its matched source."""
    derived = gate.derive_paths(_REPO_ROOT)
    for path in _COVERED_DERIVED:
        assert path in derived, f"{path} not derived — the guard would miss its drift class"
        assert derived[path], f"{path} derived with no source label"
    # mcp-entrypoint.sh is the 5d4c case: named BOTH by a Dockerfile directive and the glob.
    assert any("Dockerfile.mcp" in s for s in derived["infra/scripts/mcp-entrypoint.sh"])


# ─────────────────────────── RED: an omitted derived path is flagged ───────────────────────────


def test_red_when_a_derived_path_is_omitted() -> None:
    """Dropping a covered path from every manifest fails, naming the path + source."""
    derived = gate.derive_paths(_REPO_ROOT)
    target = "infra/scripts/mcp-entrypoint.sh"
    mutated = _real_manifest_text().replace(" " + target, "")
    tokens = gate.parse_manifest_paths(mutated)

    # It must actually have been removed from the manifest union (guards the test itself).
    assert not gate.is_covered(target, tokens)

    findings = gate.uncovered(derived, tokens, gate.EXCLUSIONS)
    flagged = {path: sources for path, sources in findings}
    assert target in flagged, "omitted derived path was not flagged — the gate fails OPEN"
    assert any("Dockerfile.mcp" in s for s in flagged[target]), "the matched source is not reported"


def test_red_findings_are_absent_when_complete() -> None:
    """The same path is NOT flagged once the manifest lists it (GREEN counterpart of RED)."""
    derived = gate.derive_paths(_REPO_ROOT)
    tokens = gate.parse_manifest_paths(_real_manifest_text())
    assert gate.uncovered(derived, tokens, gate.EXCLUSIONS) == []


@pytest.mark.parametrize("target", _COVERED_DERIVED)
def test_mutation_reomitting_any_covered_path_returns_to_red(target: str) -> None:
    """Teeth: removing ANY covered derived path individually re-triggers the gate, then restore."""
    derived = gate.derive_paths(_REPO_ROOT)
    real = _real_manifest_text()

    mutated = re.sub(r"(?<![\w/-])" + re.escape(target) + r"(?![\w/.-])", "", real)
    assert mutated != real, f"{target} not present verbatim in a manifest to mutate"

    tokens = gate.parse_manifest_paths(mutated)
    flagged = {path for path, _ in gate.uncovered(derived, tokens, gate.EXCLUSIONS)}
    assert target in flagged, f"re-omitting {target} did not return the gate to RED"

    # Restore (mutation check is non-destructive to the real tree — we only mutated a string).
    restored = gate.parse_manifest_paths(real)
    assert target not in {path for path, _ in gate.uncovered(derived, restored, gate.EXCLUSIONS)}


# ─────────────────────────── the fail-safe exclusion mechanism ───────────────────────────


def test_every_exclusion_carries_a_reason_and_names_a_real_file() -> None:
    """An exclusion without a reason, or naming no repo file, is itself a gate failure."""
    for path, reason in gate.EXCLUSIONS.items():
        assert reason.strip(), f"exclusion {path} has no reason"
        assert (_REPO_ROOT / path).is_file(), f"exclusion {path} names no repo file (stale)"


def test_exclusion_suppresses_an_otherwise_flagged_path() -> None:
    """A derived path in the exclusion list is not flagged even when in NO manifest."""
    derived = {"infra/scripts/some-derived.sh": ["glob:x"]}
    assert gate.uncovered(derived, set(), {}) == [("infra/scripts/some-derived.sh", ["glob:x"])]
    assert gate.uncovered(derived, set(), {"infra/scripts/some-derived.sh": "intentional"}) == []


def test_install_autodeploy_class_is_encoded() -> None:
    """The documented no-AUTODEPLOY_PATHS self-update exclusion is present with its rationale."""
    assert "infra/scripts/install-autodeploy.sh" in gate.EXCLUSIONS


# ─────────────────────────── manifest parsing + coverage semantics ───────────────────────────


def test_parse_manifest_paths_unions_all_paths_blocks() -> None:
    text = "FOO_PATHS='a/b c/d'\nnoise=1\nBAR_PATHS=\"e/f\"\nBAZ_PATHS='g/h'\n"
    assert gate.parse_manifest_paths(text) == {"a/b", "c/d", "e/f", "g/h"}


def test_coverage_uses_directory_prefix_semantics() -> None:
    """A directory token covers files beneath it (git-pathspec semantics), like ``src/rebar``."""
    assert gate.is_covered("src/rebar/x.py", {"src/rebar/"})
    assert gate.is_covered("src/rebar/x.py", {"src/rebar"})
    assert gate.is_covered("infra/scripts/x.sh", {"infra/scripts/x.sh"})
    assert not gate.is_covered("infra/scripts/x.sh", {"infra/scripts-other"})


# ─────────────────────────── wiring ───────────────────────────


def test_make_lint_invokes_the_gate() -> None:
    """A CI-only gate lets a local verdict be green over a tree CI rejects."""
    text = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    body: list[str] = []
    in_target = False
    for line in text.splitlines():
        if re.match(r"^lint:", line):
            in_target = True
            continue
        if in_target and re.match(r"^[A-Za-z0-9_.-]+:", line):
            break
        if in_target:
            body.append(line)
    assert "scripts/check_deploy_manifest.py" in "\n".join(body), (
        "`make lint` does not invoke the deploy-manifest gate"
    )
