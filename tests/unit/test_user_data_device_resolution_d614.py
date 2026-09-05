"""Behavioural oracle for ``user_data.sh``'s EBS device resolution (bug d614-448f-a538-4cec).

``resolve_ebs_device`` picked a device with ``nvme id-ctrl -v "$d" | grep -qi "$vol_nodash"``.
That is a SUBSTRING test that never refused degenerate input, and its answer is a
``mkfs.xfs`` target:

* an **empty** volume id made ``grep -qi ""`` match EVERY device, so the function returned
  whichever ``/dev/nvme*n1`` sorted first — and returned 0, so the caller believed it;
* a **truncated** id (``vol-0ddd`` for ``vol-0ddd3333eeee4444f``) matched by prefix.

``set -euo pipefail`` does not close this: ``-u`` catches an UNSET variable, not an EMPTY one,
and empty is the reachable case — ``infra/runbooks/review-bot-ops.md`` tells an operator to
"re-run ``user_data.sh``'s mount steps", i.e. to source these functions by hand, under incident
pressure, on a host whose other volumes hold the Gerrit data.

**These tests EXECUTE the shipped shell.** They extract the two functions verbatim from
``user_data.sh`` and run them under ``bash`` against a fake device tree with a stubbed
``nvme``/``blkid``/``mkfs.xfs``/``mount`` on ``PATH``. Only the three absolute path roots are
rewritten (``/dev/nvme*n1``, ``/dev/disk/by-id``, ``/etc/fstab``) so the fixture can own them;
the matching logic itself is the real thing. Every assertion is on OBSERVABLE behaviour — exit
status, emitted device, what ``mkfs.xfs`` was invoked with, what landed in ``fstab`` — never on
the source text, so the guards cannot be satisfied by a comment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
USER_DATA = REPO_ROOT / "infra" / "terraform" / "user_data.sh"

#: The device we plant the correct serial on. The control case must land here and nowhere else.
GOOD_VOLUME = "vol-0ddd3333eeee4444f"
GOOD_SERIAL = GOOD_VOLUME.replace("-", "")

#: The path roots the fixture takes ownership of. Each rewrite is asserted to have applied, so
#: a rename in ``user_data.sh`` fails the harness loudly instead of silently testing nothing.
_PATH_REWRITES = (
    "in /dev/nvme*n1",
    "in /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_*",
    "/etc/fstab",
)


def _shell_functions(root: Path, *, drop_lines: tuple[str, ...] = ()) -> str:
    """The two functions, verbatim, with their absolute path roots re-homed under ``root``.

    ``drop_lines`` seeds a defect: every line containing one of the given substrings is
    deleted, which is how a test proves a guard is load-bearing rather than incidental.
    """
    src = USER_DATA.read_text(encoding="utf-8")
    start = src.index("require_well_formed_volume_id() {")
    end = src.index("\n# ---", start)
    body = src[start:end]

    if drop_lines:
        kept = [ln for ln in body.splitlines() if not any(m in ln for m in drop_lines)]
        assert len(kept) < len(body.splitlines()), f"seed matched nothing: {drop_lines}"
        body = "\n".join(kept) + "\n"

    for needle in _PATH_REWRITES:
        assert needle in body, f"harness is stale: {needle!r} is no longer in user_data.sh"

    body = body.replace("in /dev/", f"in {root}/dev/")
    body = body.replace("/etc/fstab", f"{root}/etc/fstab")
    assert f"{root}/dev/nvme" in body and f"{root}/etc/fstab" in body
    return body


def _stub(path: Path, script: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + script, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def host(tmp_path: Path):
    """A fake Nitro host: three ``nvme*n1`` devices and stubbed disk tooling on ``PATH``.

    ``serials/<dev>`` drives the ``nvme`` stub, ``blkid/<dev>`` the ``blkid`` stub; both are
    plain files so a test states the world by writing one line.
    """
    root = tmp_path / "host"
    for sub in ("dev/disk/by-id", "bin", "etc", "serials", "blkid"):
        (root / sub).mkdir(parents=True)

    for name in ("nvme0n1", "nvme1n1", "nvme2n1"):
        (root / "dev" / name).write_bytes(b"\0" * 512)
    (root / "etc" / "fstab").write_text("", encoding="utf-8")

    # Only nvme1n1 carries the volume we will ask for, by both discovery routes.
    (root / "serials" / "nvme0n1").write_text("vol0aaa1111bbbb2222c\n", encoding="utf-8")
    (root / "serials" / "nvme1n1").write_text(f"{GOOD_SERIAL}\n", encoding="utf-8")
    (root / "serials" / "nvme2n1").write_text("vol0ccc9999dddd8888e\n", encoding="utf-8")
    link = root / "dev" / "disk" / "by-id" / f"nvme-Amazon_Elastic_Block_Store_{GOOD_SERIAL}"
    link.symlink_to(root / "dev" / "nvme1n1")

    _stub(
        root / "bin" / "nvme",
        f'''
# Mimic `nvme id-ctrl -v <dev>`: print an identify page whose sn field is the volume serial.
dev=$(basename "${{@: -1}}")
[ -n "$NVME_BROKEN" ] && exit 1
f="{root}/serials/$dev"
[ -f "$f" ] || exit 1
printf 'NVME Identify Controller:\\nvid : 0x1d0f\\nsn  : %s\\n' "$(cat "$f")"
printf 'mn  : Amazon Elastic Block Store\\n'
''',
    )
    _stub(
        root / "bin" / "blkid",
        f'''
# `blkid <dev>` -> exit 0 iff a signature is recorded; `blkid -s UUID -o value <dev>` -> the UUID.
dev=$(basename "${{@: -1}}")
f="{root}/blkid/$dev"
[ -f "$f" ] || exit 2
if [ "$1" = "-s" ]; then
  sed -n 's/^UUID=//p' "$f"
  exit 0
fi
cat "$f"
''',
    )
    _stub(root / "bin" / "mkfs.xfs", f'echo "$@" >> {root}/mkfs.log\n')
    _stub(root / "bin" / "mount", f'echo "$@" >> {root}/mount.log\n')
    _stub(root / "bin" / "mountpoint", "exit 0\n")

    return root


def _run(root: Path, call: str, *, drop_lines: tuple[str, ...] = (), broken_nvme: bool = False):
    """Source the extracted functions and make one call. Returns the CompletedProcess."""
    script = _shell_functions(root, drop_lines=drop_lines) + "\n" + call + "\n"
    env = dict(os.environ, PATH=f"{root}/bin:{os.environ['PATH']}")
    if broken_nvme:
        env["NVME_BROKEN"] = "1"
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


# ── AC1: an empty volume id is refused, not guessed ──────────────────────────────────


def test_empty_volume_id_is_refused_rather_than_matched_against_every_device(host) -> None:
    """AC1. ``grep -qi ''`` matches everything; the answer would be a ``mkfs.xfs`` target."""
    proc = _run(host, 'resolve_ebs_device ""')

    assert proc.returncode != 0, (
        "resolve_ebs_device accepted an EMPTY volume id and returned success — "
        f"it emitted {proc.stdout.strip()!r} as a mkfs target"
    )
    assert proc.stdout.strip() == "", "a refusal must emit no device at all"


def test_a_whitespace_only_volume_id_is_refused_too(host) -> None:
    """The same reachable shape one keystroke over: ``VOL=" "``."""
    proc = _run(host, 'resolve_ebs_device " "')
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""


# ── AC2: a truncated / prefix id is refused, not substring-matched ───────────────────


def test_a_truncated_volume_id_does_not_resolve_by_prefix(host) -> None:
    """AC2. ``vol-0ddd`` is a proper prefix of the real serial and used to resolve."""
    proc = _run(host, 'resolve_ebs_device "vol-0ddd"')

    assert proc.returncode != 0, (
        f"a truncated volume id resolved by substring match — emitted {proc.stdout.strip()!r}"
    )
    assert proc.stdout.strip() == ""


def test_a_volume_id_missing_its_prefix_is_refused(host) -> None:
    """The serial without its ``vol`` prefix is still a substring of the real one."""
    proc = _run(host, f'resolve_ebs_device "{GOOD_SERIAL[3:]}"')
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""


# ── the happy path and the proven by-id fallback must both survive ───────────────────


def test_the_correct_volume_id_still_resolves_its_own_device(host) -> None:
    """Control. The whole point of the guards is that this case is unchanged."""
    proc = _run(host, f'resolve_ebs_device "{GOOD_VOLUME}"')
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"{host}/dev/nvme1n1"


def test_the_by_id_fallback_still_resolves_when_nvme_cli_is_broken(host) -> None:
    """The fallback exists because the nvme-cli path has been observed to miss.

    Sandbox execution proved it works; the guards must not cost it. ``NVME_BROKEN`` makes
    every ``nvme`` invocation fail, exactly as the observed misses did.
    """
    proc = _run(host, f'resolve_ebs_device "{GOOD_VOLUME}"', broken_nvme=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"{host}/dev/nvme1n1"


def test_an_unknown_but_well_formed_volume_id_is_a_clean_miss(host) -> None:
    """Refusing degenerate input must not turn a legitimate "not attached yet" into a match."""
    proc = _run(host, 'resolve_ebs_device "vol-0fff7777aaaa6666b"')
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""


# ── AC3: no fstab entry when the resolved device has no filesystem UUID ──────────────


def test_a_partitioned_device_never_yields_a_malformed_fstab_entry(host) -> None:
    """AC3. ``blkid`` reports the device (so no mkfs) but has no UUID to offer.

    The old code wrote ``UUID= /var/lib/rebar/gate-scratch xfs defaults,nofail 0 2`` — a line
    that persists in ``/etc/fstab`` forever while ``mount -a`` still exits 0.
    """
    (host / "blkid" / "nvme1n1").write_text("PTTYPE=gpt\n", encoding="utf-8")
    mount_point = host / "mnt" / "gate-scratch"

    proc = _run(host, f'mount_ebs_volume "{GOOD_VOLUME}" "{mount_point}"')

    fstab = (host / "etc" / "fstab").read_text(encoding="utf-8")
    assert "UUID= " not in fstab, f"a malformed fstab entry was written: {fstab!r}"
    assert fstab.strip() == "", f"fstab must be untouched, got {fstab!r}"
    assert proc.returncode != 0, "mount_ebs_volume reported success with no filesystem UUID"
    assert not (host / "mkfs.log").exists(), "mkfs ran on a device blkid already recognised"


def test_a_healthy_device_still_gets_its_fstab_entry_and_mount(host) -> None:
    """Control for AC3: a real UUID still produces exactly one well-formed fstab line."""
    (host / "blkid" / "nvme1n1").write_text(
        "UUID=1111-2222-3333-4444\nTYPE=xfs\n", encoding="utf-8"
    )
    mount_point = host / "mnt" / "gate-scratch"

    proc = _run(host, f'mount_ebs_volume "{GOOD_VOLUME}" "{mount_point}"')

    assert proc.returncode == 0, proc.stderr
    fstab = (host / "etc" / "fstab").read_text(encoding="utf-8")
    assert fstab.strip() == f"UUID=1111-2222-3333-4444 {mount_point} xfs defaults,nofail 0 2"
    assert (host / "mount.log").read_text(encoding="utf-8").strip() == "-a"
    assert not (host / "mkfs.log").exists()


# ── AC4: the guards are load-bearing, proven by seeding the defect back in ───────────


def test_removing_the_empty_id_guard_restores_the_guessing(host) -> None:
    """AC4. Delete the refusal and the ORIGINAL defect returns, observably.

    Without this, the AC1 test could pass for an unrelated reason (a typo in the fixture, a
    ``bash`` error) and the guard could be deleted without anything going red. Seeding the
    defect proves the AC1 assertion is answered by the guard and by nothing else.
    """
    guarded = _run(host, 'resolve_ebs_device ""')
    assert guarded.returncode != 0

    seeded = _run(host, 'resolve_ebs_device ""', drop_lines=("REFUSE-DEGENERATE-VOLUME-ID",))

    assert seeded.returncode == 0, (
        "with the guard removed the empty id should resolve again — if it does not, the AC1 "
        "test is passing for some reason other than the guard"
    )
    assert seeded.stdout.strip() == f"{host}/dev/nvme0n1", (
        "the seeded defect must reproduce the reported behaviour: the first device by sort "
        f"order, got {seeded.stdout.strip()!r}"
    )


def test_removing_the_empty_uuid_guard_restores_the_malformed_fstab_line(host) -> None:
    """AC4, for AC3's guard: without it the ``UUID= `` line is written again."""
    (host / "blkid" / "nvme1n1").write_text("PTTYPE=gpt\n", encoding="utf-8")
    mount_point = host / "mnt" / "gate-scratch"

    seeded = _run(
        host,
        f'mount_ebs_volume "{GOOD_VOLUME}" "{mount_point}"',
        drop_lines=("REFUSE-EMPTY-FILESYSTEM-UUID",),
    )

    fstab = (host / "etc" / "fstab").read_text(encoding="utf-8")
    assert seeded.returncode == 0
    assert fstab.startswith("UUID= "), (
        f"the seeded defect must reproduce the malformed line, got {fstab!r}"
    )


def test_bash_is_available_for_this_oracle() -> None:
    """The oracle is worthless if it silently skips; state the dependency."""
    assert shutil.which("bash"), "these tests execute the shipped shell and need bash"
