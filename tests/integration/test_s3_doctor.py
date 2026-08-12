"""Happy-path oracle for the S3 auto-doctor (story 0289).

The minimal specification of the heal's core contract: given a ref with two divergent bundles
(each carrying a unique commit above a shared ancestor), ``heal_multi_bundle`` collapses them to
exactly one bundle whose tip reaches **every** commit from **both** heads — nothing discarded.

Contract pinned here (the implementer implements to it):

    from rebar._store.s3_doctor import heal_multi_bundle
    result = heal_multi_bundle(base_path, remote_name, ref, *, s3remote_factory=<url -> S3Remote>)

``s3remote_factory`` is the single injection seam: it maps the configured remote URL to an
``S3Remote``-like object (default: the real git_remote_s3 helper). ``result`` reports at least
``{"healed": bool, "ref": str, "merged_sha": str, "deleted_keys": list[str]}`` for logging.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest
import s3_doctor_harness as h

pytestmark = pytest.mark.integration


def _install_git_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> Path:
    """Put a fake ``git`` first on ``PATH`` that fails ``bundle create`` per *mode*.

    Injects the fault at the PROCESS boundary rather than by patching a rebar internal, so
    the test pins the observable behaviour of the heal and stays valid however the module
    reaches git. Every other git invocation is exec'd straight through to the real binary.
    Modes: ``transient-once`` (only the first bundle attempt fails, the retry succeeds),
    ``transient-always``, ``non-transient``. Returns the attempt-log path, whose line count
    is the number of ``bundle create`` invocations git actually received.
    """
    real_git = shutil.which("git")
    assert real_git, "no real git on PATH"
    shim_dir = tmp_path / "gitshim"
    shim_dir.mkdir()
    attempts = tmp_path / "bundle-attempts.log"
    shim = shim_dir / "git"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"REAL = {real_git!r}\n"
        f"ATTEMPTS = {str(attempts)!r}\n"
        f"MODE = {mode!r}\n"
        "argv = sys.argv[1:]\n"
        'if "bundle" in argv and "create" in argv:\n'
        '    with open(ATTEMPTS, "a") as fh:\n'
        '        fh.write("attempt\\n")\n'
        "    with open(ATTEMPTS) as fh:\n"
        "        n = sum(1 for _ in fh)\n"
        '    if MODE == "non-transient":\n'
        '        sys.stderr.write("fatal: Refusing to create empty bundle.\\n")\n'
        "        sys.exit(128)\n"
        '    if MODE == "transient-always" or n == 1:\n'
        # The exact stderr recorded on CI runs 31340000912 / 31349549323 / 31558315198.
        '        sys.stderr.write("fatal: bad object HEAD\\n")\n'
        "        sys.exit(128)\n"
        "os.execv(REAL, [REAL, *argv])\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    return attempts


def _bundle_attempts(attempts: Path) -> int:
    return len(attempts.read_text().splitlines()) if attempts.exists() else 0


def _seed_two_divergent_bundles(tmp_path: Path):
    """Build a common base then two divergent heads A, B; bundle each under ref 'tickets'.

    ``base_path`` is cloned from the content repo at the base commit, so it shares the EXACT
    common ancestor of both bundle heads. Returns (remote, base_path, sha_base, sha_a, sha_b)."""
    objdir = tmp_path / "objstore"
    remote = h.FakeS3Remote(objdir)

    content = h.init_repo(tmp_path / "content")
    sha_base = h.commit_file(content, "base.txt", "base", "base")

    # base_path shares the exact base commit (clone at base, before A/B exist).
    h.git(tmp_path, "clone", "-q", "-b", "tickets", str(content), "tracker")
    base_path = tmp_path / "tracker"
    h.git(base_path, "remote", "remove", "origin")
    h.git(base_path, "remote", "add", "origin", "s3://test-bucket/tickets")

    # Head A above base.
    h.commit_file(content, "a.txt", "A", "commit A")
    sha_a = h.seed_bundle(remote, content, "tickets", "tickets")

    # Reset to base, build head B above base (divergent from A).
    h.git(content, "reset", "-q", "--hard", sha_base)
    h.commit_file(content, "b.txt", "B", "commit B")
    sha_b = h.seed_bundle(remote, content, "tickets", "tickets")

    return remote, base_path, sha_base, sha_a, sha_b


def test_heal_collapses_two_bundles_losslessly(tmp_path: Path) -> None:
    """Two divergent bundles -> exactly one bundle, both unique commits reachable, clone works."""
    remote, base_path, _sha_base, sha_a, sha_b = _seed_two_divergent_bundles(tmp_path)

    assert len(h.bundle_keys(remote, "tickets")) == 2  # precondition: the multi-bundle state

    from rebar._store.s3_doctor import heal_multi_bundle

    result = heal_multi_bundle(
        str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
    )

    # Postcondition 1: collapsed to exactly one bundle.
    assert len(h.bundle_keys(remote, "tickets")) == 1
    assert result["healed"] is True

    # Postcondition 2: a fresh clone of the single healed bundle succeeds, and every unique
    # commit from BOTH heads is reachable from the healed tip (no head discarded).
    clone, _tip = h.healed_tip(remote, "tickets", tmp_path)
    assert h.reachable(clone, "HEAD", sha_a), "commit A dropped by the heal"
    assert h.reachable(clone, "HEAD", sha_b), "commit B dropped by the heal"


def test_heal_rides_out_a_transient_bad_object_from_the_bundle_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient object-DB read fault on the bundle step self-heals on retry.

    Bug wrongful-chemic-squeaker: three CI runs failed the heal with ``fatal: bad object
    HEAD`` while bundling a merged tip whose sha git had just printed — git resolved the
    name to an OID but could not READ the object at that instant. The doctor's git helper
    sat outside rebar's self-healing seam, so a blip that every other store write rides out
    aborted the heal. The first bundle attempt here fails with the exact CI stderr; the
    identical retry succeeds, exactly as the seam's idempotency precondition allows.
    """
    remote, base_path, _sha_base, sha_a, sha_b = _seed_two_divergent_bundles(tmp_path)
    attempts = _install_git_shim(tmp_path, monkeypatch, "transient-once")

    from rebar._store.s3_doctor import heal_multi_bundle

    result = heal_multi_bundle(
        str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
    )

    assert result["healed"] is True
    assert _bundle_attempts(attempts) >= 2, "the failed bundle step was never retried"
    assert len(h.bundle_keys(remote, "tickets")) == 1
    clone, _tip = h.healed_tip(remote, "tickets", tmp_path)
    assert h.reachable(clone, "HEAD", sha_a), "commit A dropped by the retried heal"
    assert h.reachable(clone, "HEAD", sha_b), "commit B dropped by the retried heal"


def test_heal_does_not_retry_a_non_transient_bundle_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git failure OUTSIDE the transient signature still surfaces at once, unretried.

    The guard on the retry above: the marker set must stay narrow enough that a genuine
    error is neither masked nor delayed.
    """
    remote, base_path, _sha_base, _sha_a, _sha_b = _seed_two_divergent_bundles(tmp_path)
    attempts = _install_git_shim(tmp_path, monkeypatch, "non-transient")

    from rebar._store.s3_doctor import S3DoctorConflict, heal_multi_bundle

    with pytest.raises(S3DoctorConflict):
        heal_multi_bundle(
            str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
        )

    assert _bundle_attempts(attempts) == 1, "a non-transient failure must not be retried"


def test_persistent_bad_object_reports_an_object_read_failure_not_a_heads_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the transient never clears, the error names the real fault.

    ``S3DoctorConflict``'s default hint sends the operator to ``rebar fsck-recover``, which
    is the wrong tool for a merge that never conflicted: the merge succeeded and the object
    store could not be READ. The raised error distinguishes the two.
    """
    remote, base_path, _sha_base, _sha_a, _sha_b = _seed_two_divergent_bundles(tmp_path)
    attempts = _install_git_shim(tmp_path, monkeypatch, "transient-always")

    from rebar._store.s3_doctor import S3DoctorConflict, heal_multi_bundle

    with pytest.raises(S3DoctorConflict) as excinfo:
        heal_multi_bundle(
            str(base_path), "origin", "tickets", s3remote_factory=h.make_factory(remote)
        )

    assert _bundle_attempts(attempts) > 1, "the transient signature was never retried"
    hint = excinfo.value.hint
    assert "fsck-recover" not in hint, "an unreadable object is not a heads conflict"
    assert "object" in hint.lower()
