"""Pin the GitHub mirror-lock Terraform contract (8ccf).

Release tags stay human-pushable. Missing deploy keys must fail apply through
a resource precondition. The main lock blocks direct pushes and PR merges, with
only the replication deploy key bypassing it. These offline text checks guard
committed IaC. Deployed Terraform drift is checked separately."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MAIN_TF = Path(__file__).resolve().parents[2] / "infra" / "terraform-github" / "main.tf"


def _main_lock_block() -> str:
    """The text of the module from the ``main_lock`` resource onward (main-only, so this
    is the only ruleset resource; the trailing ``output`` block is harmless to include)."""
    tf = _MAIN_TF.read_text()
    marker = 'resource "github_repository_ruleset" "main_lock"'
    assert marker in tf, "main_lock ruleset resource is missing"
    return tf.split(marker, 1)[1]


def test_module_is_main_only_no_tag_lock() -> None:
    tf = _MAIN_TF.read_text()
    assert 'resource "github_repository_ruleset" "main_lock"' in tf
    # tags stay human-pushable (release.yml v* pushes) -> NO tag lock ruleset
    assert 'resource "github_repository_ruleset" "tag_lock"' not in tf
    assert 'target      = "tag"' not in tf
    assert 'include = ["refs/tags/**"]' not in tf
    assert 'output "tag_lock_ruleset_id"' not in tf


def test_deploy_key_gate_is_a_fail_closed_precondition_not_a_check() -> None:
    tf = _MAIN_TF.read_text()
    # a `check` block only emits a WARNING and lets apply proceed — it must be gone.
    assert 'check "deploy_key_present"' not in tf
    # the gate must live as a lifecycle precondition INSIDE the main_lock resource so a
    # missing deploy key ABORTS plan/apply (never activate a lock whose only bypass is absent).
    block = _main_lock_block()
    assert "lifecycle {" in block
    after_lifecycle = block.split("lifecycle {", 1)[1]
    assert "precondition {" in after_lifecycle
    assert "var.deploy_key_title" in after_lifecycle


def test_main_lock_enforces_the_full_lock_contract() -> None:
    block = _main_lock_block()
    assert 'enforcement = "active"' in block
    assert 'target      = "branch"' in block
    assert 'include = ["refs/heads/main"]' in block
    # the rules that reject direct pushes + PR merges (update), deletion, and force-push
    assert re.search(r"\bupdate\s*=\s*true", block)
    assert re.search(r"\bdeletion\s*=\s*true", block)
    assert re.search(r"\bnon_fast_forward\s*=\s*true", block)
    # sole bypass actor is the replication deploy key, with NO numeric actor_id
    assert 'actor_type  = "DeployKey"' in block
    assert 'bypass_mode = "always"' in block
    assert not re.search(r"actor_id\s*=", block), "a DeployKey bypass must carry no actor_id"
