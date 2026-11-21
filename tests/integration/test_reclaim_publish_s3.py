"""S3 bundle-set publish-swap: exclusive, T0-checked, backed up, doctor-free (ADR 0106 §6).

Runs against ``s3_doctor_harness.FakeS3Remote`` (real git bundles in a local object
directory), never AWS.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import s3_doctor_harness as h

from rebar._commands import reclaim_publish as publish_swap

pytestmark = pytest.mark.integration

REF = "tickets"


def _shadow(tmp_path: Path) -> tuple[Path, str, str]:
    """A shadow clone holding T0 and a parentless rewrite of it."""
    shadow = h.init_repo(tmp_path / "shadow")
    h.commit_file(shadow, "a.json", "{}\n", "first")
    t0 = h.commit_file(shadow, "b.json", "{}\n", "second")
    tree = h.git(shadow, "rev-parse", f"{t0}^{{tree}}").stdout.strip()
    new_tip = h.git(shadow, "commit-tree", tree, "-m", "checkpoint").stdout.strip()
    (shadow / ".rebar").mkdir()
    (shadow / ".rebar" / "reclaim-shadow-clone").write_text("disposable\n", encoding="utf-8")
    return shadow, t0, new_tip


def _verify_dir(tmp_path: Path) -> Path:
    scratch = tmp_path / "verify"
    scratch.mkdir(exist_ok=True)
    return scratch


def _remote_with(tmp_path: Path, shadow: Path, *tips: str) -> h.FakeS3Remote:
    remote = h.FakeS3Remote(tmp_path / "bucket")
    for i, tip in enumerate(tips):
        h.git(shadow, "branch", "-f", f"seed{i}", tip)
        h.seed_bundle(remote, shadow, REF, f"seed{i}")
    return remote


def test_bundle_swap_replaces_the_set_and_keeps_a_rollback_copy(tmp_path) -> None:
    shadow, t0, new_tip = _shadow(tmp_path)
    remote = _remote_with(tmp_path, shadow, t0)
    original = h.bundle_keys(remote, REF)
    original_bytes = {k.rsplit("/", 1)[-1]: (remote.objdir / k).read_bytes() for k in original}

    result = publish_swap.publish_s3_bundle_set(
        shadow, remote, ref=REF, expected_heads={t0}, new_tip=new_tip
    )

    assert h.bundle_keys(remote, REF) == [result.new_key]
    _, tip = h.healed_tip(remote, REF, _verify_dir(tmp_path))
    assert tip == new_tip
    assert result.deleted_keys == tuple(original)
    assert len(result.backed_up_keys) == len(original)
    for key in result.backed_up_keys:
        assert key.startswith(result.rollback_prefix + "/")
        assert (remote.objdir / key).read_bytes() == original_bytes[key.rsplit("/", 1)[-1]]
    # The rollback copies are invisible to the helper's ref enumeration.
    assert all("PROTECTED#" not in k for k in h.bundle_keys(remote, REF))
    assert not (remote.objdir / remote.prefix / REF / "LOCK#.lock").exists()


def test_bundle_swap_aborts_without_writing_when_heads_moved(tmp_path) -> None:
    shadow, t0, new_tip = _shadow(tmp_path)
    other = h.commit_file(shadow, "c.json", "{}\n", "concurrent push")
    remote = _remote_with(tmp_path, shadow, t0, other)
    before = sorted(p.relative_to(remote.objdir) for p in remote.objdir.rglob("*") if p.is_file())

    with pytest.raises(publish_swap.PublishAborted):
        publish_swap.publish_s3_bundle_set(
            shadow, remote, ref=REF, expected_heads={t0}, new_tip=new_tip
        )

    after = sorted(p.relative_to(remote.objdir) for p in remote.objdir.rglob("*") if p.is_file())
    assert after == before


def test_bundle_swap_aborts_when_the_ref_lock_is_held(tmp_path) -> None:
    shadow, t0, new_tip = _shadow(tmp_path)
    remote = _remote_with(tmp_path, shadow, t0)
    held = remote.acquire_lock(REF)
    assert held is not None

    with pytest.raises(publish_swap.PublishAborted):
        publish_swap.publish_s3_bundle_set(
            shadow, remote, ref=REF, expected_heads={t0}, new_tip=new_tip
        )

    assert h.bundle_keys(remote, REF) == [f"{remote.prefix}/{REF}/{t0}.bundle"]


def test_doctor_after_swap_does_not_merge_the_old_history_back(tmp_path) -> None:
    from rebar._store import s3_doctor

    shadow, t0, new_tip = _shadow(tmp_path)
    remote = _remote_with(tmp_path, shadow, t0)
    publish_swap.publish_s3_bundle_set(
        shadow, remote, ref=REF, expected_heads={t0}, new_tip=new_tip
    )

    client = h.init_repo(tmp_path / "client")
    h.git(client, "remote", "add", "origin", "s3://test-bucket/tickets")
    healed = s3_doctor.heal_multi_bundle(
        str(client), "origin", REF, s3remote_factory=h.make_factory(remote)
    )

    assert healed["healed"] is False
    assert healed["reason"] == "noop"
    clone, tip = h.healed_tip(remote, REF, _verify_dir(tmp_path))
    assert tip == new_tip
    assert not h.reachable(clone, "HEAD", t0)
