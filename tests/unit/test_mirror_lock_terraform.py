"""Contractual invariants for the GitHub mirror-lock IaC (ticket 8ccf, epic b744).

Pins the ``infra/terraform-github`` module to the WS7 cutover decisions so a future edit
cannot SILENTLY:

- reintroduce a tag lock (tags MUST stay human-pushable so ``.github/workflows/release.yml``
  can publish to PyPI on a ``v*`` tag push),
- downgrade the fail-closed deploy-key existence gate back to a ``check`` block (which only
  WARNs and lets ``apply`` proceed) instead of a resource ``precondition`` (which ABORTS), or
- weaken the ``main`` lock's rules / bypass actor (the lock rejects direct pushes AND PR merges
  and its sole bypass is the replication deploy key).

This is a text-contract test on the committed IaC — offline and fast. The live reconcile
(``terraform validate`` + ``terraform plan`` == "No changes" against the imported live ruleset)
is verified out-of-band at cutover, not here.
"""

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
