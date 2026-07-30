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

import shutil
import subprocess
import uuid
from pathlib import Path

# ── store template: build once, copy per test ────────────────────────────────
#
# Building a store costs ~306 ms and 58 git process spawns (``init_repo`` alone is
# ~268 ms / 54 spawns), and 589 tests in this tier request one. Copying a prebuilt
# template costs ~22 ms — the tier is spawn-bound, not IO-bound, so this is the
# single largest lever on the macOS CI cell (the Verified gate's critical path).
#
# A raw copy is NOT a usable store. ``init_repo`` makes the ticket store a git
# LINKED WORKTREE, so the copy must be re-pointed at itself. See _clone_template.

#: The two files inside a built store that embed its own absolute path. Found by
#: scanning every file (binaries included, no size limit) of a freshly built store.
#: ``commondir`` is relative and ``.git/config`` carries no paths, so these are the
#: complete set *for the current* ``init_repo``. ``assert_store_self_contained``
#: does NOT rely on this list — it re-derives containment from git itself — so a
#: third pointer added by a future ``init_repo`` fails loudly instead of silently.
_WORKTREE_POINTERS = (".tickets-tracker/.git", ".git/worktrees/-tickets-tracker/gitdir")

#: Per-store identity minted by ``init_repo``. Copied verbatim, so it must be
#: re-minted per test or every store in a worker shares one environment identity.
_IDENTITY_FILES = ((".tickets-tracker/.env-id", 0o644), (".tickets-tracker/.signing-key", 0o600))


def worktree_paths(repo: Path) -> list[Path]:
    """Every worktree path git itself associates with *repo*'s repository."""
    out = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [Path(ln.split(" ", 1)[1]) for ln in out.splitlines() if ln.startswith("worktree ")]


def assert_store_self_contained(repo: Path) -> None:
    """Fail unless every worktree git associates with *repo* lives inside *repo*.

    This is the guard against the template scheme's one catastrophic failure mode:
    a copy that still points at the template's object store and ``refs/heads/tickets``.
    A write in such a copy advances the TEMPLATE's ref, so under ``-n 3`` every worker
    shares one ref and one object database — an unreproducible flake spray.

    It is deliberately NOT ``rev-parse --git-common-dir``. That value derives from
    ``.tickets-tracker/.git`` alone, so a copy with a stale ``gitdir`` pointer passes
    it while ``worktree list`` still names the template — green while broken. Verified
    by tests/interfaces/test_rebar_repo_isolation.py, which pins exactly that case.
    """
    root = repo.resolve()
    stray = [p for p in worktree_paths(repo) if root not in (p.resolve(), *p.resolve().parents)]
    if stray:
        raise AssertionError(
            f"store at {root} references worktrees outside itself: {stray}. "
            "A copied store was not re-pointed at itself; it is sharing another "
            "store's object database and refs."
        )


def _clone_template(template: Path, dest: Path) -> Path:
    """Copy *template* to *dest* and make the copy a genuinely independent store."""
    # Check the SOURCE first. A copy is only as isolated as what it was copied from,
    # and the template is reachable by every test in the worker. This is not
    # hypothetical: `git worktree repair` run inside a copy rewrites the SOURCE's
    # pointer to aim at that copy, so one careless test can redirect the template and
    # silently corrupt every later test. Failing here names the culprit's successor
    # immediately instead of surfacing as a confusing ref mismatch much later.
    assert_store_self_contained(template)
    shutil.copytree(template, dest, symlinks=True)
    # Resolve BOTH sides: macOS reports /private/var/... while tmp paths read
    # /var/..., and the pointer files store the resolved form.
    src_s, dst_s = str(template.resolve()), str(dest.resolve())
    for rel in _WORKTREE_POINTERS:
        p = dest / rel
        if p.exists():
            p.write_text(p.read_text(encoding="utf-8").replace(src_s, dst_s), encoding="utf-8")

    # Re-mint per-store identity. These writes MUST be unconditional: the obvious
    # helper, init._gen_local_files(), guards both writes with `if not os.path.isfile`,
    # so on a copy (where both already exist) it is a SILENT no-op and every store
    # would keep the template's identity. Pinned by test_rebar_repo_isolation.py.
    for rel, mode in _IDENTITY_FILES:
        p = dest / rel
        if p.exists():
            p.write_text(f"{uuid.uuid4()}\n", encoding="utf-8")
            p.chmod(mode)

    assert_store_self_contained(dest)
    return dest
