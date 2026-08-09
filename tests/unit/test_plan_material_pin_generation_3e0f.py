"""Related pins remain valid across supported fingerprint generations."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar.llm.plan_review import attest
from rebar.llm.plan_review.relation_snapshot import (
    PlanMaterialPin,
    current_material_fingerprint_impl,
)

pytestmark = pytest.mark.unit

_DESCRIPTION = "## Plan\nDo the thing.  \n\n## Acceptance Criteria\n- [ ] done\n"


@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    monkeypatch.chdir(repo)
    return repo


def _historical_pin(repo: Path) -> tuple[str, PlanMaterialPin]:
    root = str(repo)
    related = rebar.create_ticket(
        "task", "Pinned prerequisite", description=_DESCRIPTION, repo_root=root
    )
    latest = current_material_fingerprint_impl(related, repo_root=root)
    historical = current_material_fingerprint_impl(
        related, repo_root=root, normalize_whitespace=False
    )
    assert latest is not None and historical is not None
    assert latest != historical
    return related, PlanMaterialPin("prerequisite", related, historical)


def test_unchanged_historical_generation_pin_is_current(rebar_repo: Path) -> None:
    related, pin = _historical_pin(rebar_repo)
    assert rebar.show_ticket(related, repo_root=str(rebar_repo))["description"] == _DESCRIPTION

    health = attest.derive_plan_material_pin_health(
        (pin,), repo_root=str(rebar_repo), enforced=True
    )

    assert health["pin_status"] == "current"
    assert health["targets"][0]["pin_status"] == "current"
    assert health["targets"][0]["pinned_fingerprint"] == pin.material_fingerprint


def test_semantic_edit_keeps_historical_pin_stale(rebar_repo: Path) -> None:
    related, pin = _historical_pin(rebar_repo)
    rebar.edit_ticket(
        related,
        description=_DESCRIPTION + "\nAlso replace the storage layer.\n",
        repo_root=str(rebar_repo),
    )

    health = attest.derive_plan_material_pin_health(
        (pin,), repo_root=str(rebar_repo), enforced=True
    )

    assert health["pin_status"] == "stale-pin-drift"
    assert health["targets"][0]["pin_status"] == "stale-pin-drift"


def test_unmatched_valid_hash_stays_stale(rebar_repo: Path) -> None:
    related, _ = _historical_pin(rebar_repo)
    pin = PlanMaterialPin("prerequisite", related, "0000000000000000")

    health = attest.derive_plan_material_pin_health(
        (pin,), repo_root=str(rebar_repo), enforced=True
    )

    assert health["pin_status"] == "stale-pin-drift"
    assert health["targets"][0]["pin_status"] == "stale-pin-drift"
