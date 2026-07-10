"""AC0 live proof (task 524d / epic dust-troth-naval): a blob-pointing
refs/reconciler/* ref round-trips push+fetch through the REAL origin remote.

The hermetic counterpart (tests/unit/rebar_reconciler/state/test_ref_lock.py::
test_ac0_blob_ref_roundtrips_through_remote) proves the same mechanics against a
local bare remote. This external-tier test proves it against the actual `origin`
(GitHub) — the environment AC0 names — so the completion gate has a live artifact,
not just a bare-remote analogue. It pushes a throwaway scratch ref under
refs/reconciler/spike-* and always deletes it (local + remote) in teardown.

Marked ``external`` (excluded from the default run; needs REBAR_RUN_EXTERNAL=1).
Run locally / in CI::

    REBAR_RUN_EXTERNAL=1 pytest -m external tests/external/test_ref_lock_ac0_live.py
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.external

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
REF_LOCK_PATH = ENGINE_DIR / "rebar_reconciler" / "_ref_lock.py"
REMOTE = os.environ.get("REBAR_AC0_REMOTE", "origin")


def _load_ref_lock() -> ModuleType:
    # _ref_lock loads by file path, but its git ops defer-import the sibling package
    # (``from rebar_reconciler import git_adapter``, added in d9e0f0e7). The bare
    # ``rebar_reconciler`` name only resolves with src/rebar/_engine on sys.path — the
    # same setup the other reconciler-touching external tests already carry (see
    # test_link_sync_roundtrip_live._ensure_engine_on_path).
    if str(ENGINE_DIR) not in sys.path:
        sys.path.insert(0, str(ENGINE_DIR))
    spec = importlib.util.spec_from_file_location("rebar_reconciler_ref_lock_live", REF_LOCK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rebar_reconciler_ref_lock_live"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _has_remote() -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "remote"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return REMOTE in out


@pytest.mark.skipif(not _has_remote(), reason=f"no '{REMOTE}' remote configured")
def test_ac0_blob_ref_roundtrips_through_real_origin() -> None:
    """acquire -> ls-remote -> read(remote) -> release, all against the real origin."""
    rl = _load_ref_lock()
    ref = f"refs/reconciler/spike-ac0-{os.getpid()}"
    oid = None
    try:
        oid = rl.acquire(REPO_ROOT, ref, holder="ac0-live", lease_secs=120, remote=REMOTE)

        # The real remote advertises the ref at exactly the blob OID.
        ls = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-remote", REMOTE, ref],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        assert ls and ls[0] == oid, f"remote ref {ls!r} != acquired oid {oid}"

        state = rl.read(REPO_ROOT, ref, remote=REMOTE)
        assert state is not None and state.holder == "ac0-live" and state.fence == 0
    finally:
        # Always tear down the scratch ref (local + remote), even on failure.
        if oid is not None:
            rl.release(REPO_ROOT, ref, oid=oid, remote=REMOTE)
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "update-ref", "-d", ref],
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "push", REMOTE, f":{ref}"],
            capture_output=True,
        )
