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

from pathlib import Path

import pytest

from tests.integration import s3_doctor_harness as h

pytestmark = pytest.mark.integration


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
