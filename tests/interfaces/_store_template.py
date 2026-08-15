"""Store-template helpers for the interfaces tier.

These live in their own module rather than in ``conftest.py`` on purpose. A
conftest is importable as the bare name ``conftest``, but that name is NOT stable:
several conftests in this repo share it, and under xdist ``sys.modules["conftest"]``
ends up bound to whichever one was imported most recently. A test that does
``from conftest import ...`` therefore works when its own tier runs alone and fails
in a full-suite run — which is exactly how this bit once broke CI. Importing from a
uniquely-named module removes the ambiguity.
"""

from __future__ import annotations

from pathlib import Path

from _topology_template import (
    assert_store_self_contained as assert_store_self_contained,
)
from _topology_template import (
    clone_topology_template,
)
from _topology_template import (
    worktree_paths as worktree_paths,
)

# Compatibility constants remain part of this helper's test-facing contract.
# Path repair itself is centralized in ``clone_topology_template`` so broader
# topologies (multiple stores and sibling bare remotes) use the same safe seam.
_WORKTREE_POINTERS = (".tickets-tracker/.git", ".git/worktrees/-tickets-tracker/gitdir")
_IDENTITY_FILES = ((".tickets-tracker/.env-id", 0o644), (".tickets-tracker/.signing-key", 0o600))

# ── store template: build once, copy per test ────────────────────────────────
#
# Building a store costs ~306 ms and 58 git process spawns (``init_repo`` alone is
# ~268 ms / 54 spawns), and 589 tests in this tier request one. Copying a prebuilt
# template costs ~22 ms — the tier is spawn-bound, not IO-bound, so this is the
# single largest lever on the macOS CI cell (the Verified gate's critical path).
#
# A raw copy is NOT a usable store. ``init_repo`` makes the ticket store a git
# LINKED WORKTREE, so the copy must be re-pointed at itself. See _clone_template.


def _clone_template(template: Path, dest: Path) -> Path:
    """Copy *template* to *dest* and make the copy a genuinely independent store."""
    return clone_topology_template(template, dest)
