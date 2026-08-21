"""``clone_topology_template`` must refuse a template holding unpinned bare upkeep.

``clone_topology_template`` is the only mechanism in ``tests/`` that copies a whole
topology — a ``shutil.copytree`` plus two full-tree ``rglob("*")`` + ``read_bytes()``
passes. That makes it the amplifier for bug b394-6198-6010-42f7: a bare remote inside
the template that is still running detached background upkeep is repacked and pruned
concurrently with those walks, and the copy raises on an entry that vanished.

Rather than sweep three git config keys across every fixture that happens to build a
bare remote today — an enumeration that decays the moment someone adds another — the
amplifier itself refuses to copy such a template. Any future fixture that acquires a
copy step converts a latent, load-dependent flake into a deterministic setup failure
that names the repository and the keys it is missing.

These tests cover the precondition in BOTH directions: it must fire on an unpinned
template, and it must not fire once the same template is pinned. A guard only tested
on the failing side could be a guard that always fires.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _git_upkeep import BARE_REMOTE_UPKEEP_PINS, apply_upkeep_pins
from _topology_template import clone_topology_template


def _unpinned_topology(root: Path) -> Path:
    """A template holding a bare repository created the raw way — no pins at all."""
    template = root / "template"
    remote = template / "origin.git"
    remote.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    return template


def test_refuses_a_template_holding_an_unpinned_bare_repository(tmp_path: Path) -> None:
    """The copy must not start against a repository something may still be rewriting."""
    template = _unpinned_topology(tmp_path)

    with pytest.raises(AssertionError) as excinfo:
        clone_topology_template(template, tmp_path / "copy")

    message = str(excinfo.value)
    assert "origin.git" in message, "the refusal must name the offending repository"
    for key in BARE_REMOTE_UPKEEP_PINS:
        assert key in message, f"the refusal must name the missing pin {key}"
    assert not (tmp_path / "copy").exists(), (
        "the precondition must refuse BEFORE copying, not clean up afterwards"
    )


def test_accepts_that_same_template_once_it_is_pinned(tmp_path: Path) -> None:
    """The same template, pinned, copies normally — the guard is not a blanket refusal."""
    template = _unpinned_topology(tmp_path)
    apply_upkeep_pins(template / "origin.git")

    destination = clone_topology_template(template, tmp_path / "copy")

    assert destination == tmp_path / "copy"
    assert (destination / "origin.git" / "HEAD").is_file(), "the bare repository was copied"


def test_a_partially_pinned_repository_is_still_refused(tmp_path: Path) -> None:
    """Every pin is load-bearing, so a subset must not satisfy the precondition."""
    template = _unpinned_topology(tmp_path)
    apply_upkeep_pins(template / "origin.git")
    dropped = sorted(BARE_REMOTE_UPKEEP_PINS)[0]
    subprocess.run(
        ["git", "--git-dir", str(template / "origin.git"), "config", "--unset", dropped],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(AssertionError) as excinfo:
        clone_topology_template(template, tmp_path / "copy")

    assert dropped in str(excinfo.value)
