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

This module has since become the single behavioural oracle for ``user_data.sh``'s mount path,
because a second harness that extracted the same functions would drift from this one. It also
covers:

* **bug 9c93-754e-b641-48d1** — ``mount -a`` exits 0 for a ``nofail`` entry it SKIPPED, so a
  successful ``mount -a`` was never evidence that the mount took. Gate scratch asserted it;
  ``/var/gerrit``, the more valuable volume, asserted nothing at all.
* **bug ad8d-4274-ef43-4f44** — the fstab guard tested only the NEW UUID, so replacing a volume
  appended a second entry for the same mount point (F3); and the two resolution branches
  returned different string forms for the same device (F5).

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
from _subprocess_env import subprocess_env

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
    root = tmp_path.resolve() / "host"
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
    # `mount -a` has THREE outcomes and the whole of bug 9c93 lives in the difference between
    # the last two, so the stub models all three rather than always succeeding:
    #   default          — the entries mount; `mounted` gains their mount points.
    #   MOUNT_ERRORS=1   — mount refuses and says so (exit 32, mount(8)'s "mount failure").
    #   MOUNT_SKIPS=1    — mount exits 0 having mounted NOTHING. That is not a contrived
    #                      stub: it is precisely what `nofail` does for a missing volume,
    #                      and it is the mode under which the old script reported success.
    _stub(
        root / "bin" / "mount",
        f"""
echo "$@" >> {root}/mount.log
[ -n "${{MOUNT_ERRORS:-}}" ] && exit 32
[ -n "${{MOUNT_SKIPS:-}}" ] && exit 0
awk '$1 ~ /^UUID=/ {{ print $2 }}' {root}/etc/fstab >> {root}/mounted
exit 0
""",
    )
    _stub(
        root / "bin" / "mountpoint",
        f'grep -qxF "$2" {root}/mounted 2>/dev/null\n',
    )

    return root


def _run(
    root: Path,
    call: str,
    *,
    drop_lines: tuple[str, ...] = (),
    broken_nvme: bool = False,
    mount_mode: str = "",
):
    """Source the extracted functions and make one call. Returns the CompletedProcess.

    ``mount_mode`` selects the ``mount -a`` outcome: ``""`` mounts, ``"skips"`` is the silent
    ``nofail`` skip, ``"errors"`` is a genuine mount failure.
    """
    script = _shell_functions(root, drop_lines=drop_lines) + "\n" + call + "\n"
    # `subprocess_env` rather than `dict(os.environ, ...)`: pytest renders call arguments in a
    # long traceback, so a plain dict of the ambient environment would print every inherited
    # secret if bash failed to start (tests/_subprocess_env.py; the whole-tree scan in
    # tests/unit/test_subprocess_env_repr_security.py enforces it).
    env = subprocess_env(PATH=f"{root}/bin:{os.environ['PATH']}")
    if broken_nvme:
        env["NVME_BROKEN"] = "1"
    if mount_mode == "skips":
        env["MOUNT_SKIPS"] = "1"
    elif mount_mode == "errors":
        env["MOUNT_ERRORS"] = "1"
    elif mount_mode:
        raise AssertionError(f"unknown mount_mode {mount_mode!r}")
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


# ── bug 9c93: the mount is ASSERTED, for BOTH volumes ────────────────────────────────


def _healthy(host: Path) -> Path:
    """A resolvable device with a real filesystem UUID. Returns the mount point to use."""
    (host / "blkid" / "nvme1n1").write_text(
        "UUID=1111-2222-3333-4444\nTYPE=xfs\n", encoding="utf-8"
    )
    return host / "mnt" / "gerrit"


def test_a_silently_skipped_nofail_mount_is_refused(host) -> None:
    """9c93 AC1/AC2. ``mount -a`` exits 0 having mounted nothing — the real ``nofail`` skip.

    The fstab entry is written, ``mount -a`` succeeds, and the mount point is still an
    ordinary directory on the root filesystem. Before this fix the script reported success
    here for ``/var/gerrit``, which is how Gerrit could come back from a stop/start writing
    its repositories to the 60 GiB root disk with nothing raising.
    """
    mount_point = _healthy(host)

    proc = _run(host, f'mount_ebs_volume "{GOOD_VOLUME}" "{mount_point}"', mount_mode="skips")

    assert proc.returncode != 0, (
        "mount_ebs_volume reported SUCCESS for a mount that never happened — the volume's "
        "consumers would run on the root filesystem"
    )
    assert "not a mount point" in proc.stderr.lower(), proc.stderr


def test_the_gerrit_data_volume_gets_the_same_assertion_as_gate_scratch(host) -> None:
    """9c93. The bug was the ASYMMETRY, so assert the guard is per-volume, not per-call-site.

    ``require_mounted`` lives inside ``mount_ebs_volume``, so it cannot be true of one volume
    and absent for another — which is exactly how ``/var/gerrit`` came to have no assertion
    while gate scratch had one.
    """
    for name in ("gerrit", "gate-scratch"):
        (host / "blkid" / "nvme1n1").write_text("UUID=aaaa-bbbb\n", encoding="utf-8")
        (host / "etc" / "fstab").write_text("", encoding="utf-8")
        (host / "mounted").unlink(missing_ok=True)
        mount_point = host / "mnt" / name

        proc = _run(host, f'mount_ebs_volume "{GOOD_VOLUME}" "{mount_point}"', mount_mode="skips")

        assert proc.returncode != 0, f"{name}: a skipped mount was reported as success"


def test_a_genuine_mount_error_is_distinguishable_from_a_silent_skip(host) -> None:
    """9c93 AC3. Two failures that look identical from outside must NOT read identically.

    ``nofail`` is why: a mount that errored printed something, a mount that was skipped
    printed nothing at all. An operator needs to know whether to look at the filesystem or at
    the volume attachment, so the two get different messages AND different exit codes.
    """
    mount_point = _healthy(host)
    call = f'mount_ebs_volume "{GOOD_VOLUME}" "{mount_point}"'

    errored = _run(host, call, mount_mode="errors")
    (host / "etc" / "fstab").write_text("", encoding="utf-8")
    skipped = _run(host, call, mount_mode="skips")

    assert errored.returncode != 0 and skipped.returncode != 0
    assert errored.returncode != skipped.returncode, (
        "a mount error and a silent nofail skip returned the SAME status — they need "
        f"different operator responses (both were {errored.returncode})"
    )
    assert "reported an error" in errored.stderr.lower(), errored.stderr
    assert "not a mount point" in skipped.stderr.lower(), skipped.stderr
    assert "not a mount point" not in errored.stderr.lower()


def test_a_mount_that_actually_takes_still_succeeds(host) -> None:
    """Control for the assertion: the guard must not cost the working case."""
    mount_point = _healthy(host)

    proc = _run(host, f'mount_ebs_volume "{GOOD_VOLUME}" "{mount_point}"')

    assert proc.returncode == 0, proc.stderr


def test_removing_the_mount_assertion_restores_the_silent_success(host) -> None:
    """9c93 AC2, seeded. Delete the assertion and the reported defect returns, observably.

    This is the test that gives the others teeth. The whole nature of 9c93 is that an ABSENT
    assertion is invisible — so a test that would pass without the assertion proves nothing.
    """
    mount_point = _healthy(host)
    call = f'mount_ebs_volume "{GOOD_VOLUME}" "{mount_point}"'

    guarded = _run(host, call, mount_mode="skips")
    assert guarded.returncode != 0

    (host / "etc" / "fstab").write_text("", encoding="utf-8")
    seeded = _run(host, call, mount_mode="skips", drop_lines=("ASSERT-MOUNT-TOOK",))

    assert seeded.returncode == 0, (
        "with the assertion removed the skipped mount should be reported as SUCCESS again — "
        "if it is not, the tests above are passing for some reason other than the assertion"
    )


# ── bug ad8d F3: an fstab entry is REPLACED, not accumulated ─────────────────────────

#: An unrelated entry that must survive every rewrite. If a fix drops this, the host does not
#: boot, so it is the more important half of the assertion.
ROOT_FSTAB_LINE = "UUID=0000-1111 / xfs defaults,noatime 0 1"


def _fstab_lines(host: Path, mount_point: Path) -> list[str]:
    text = (host / "etc" / "fstab").read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if f" {mount_point} " in ln]


def test_replacing_a_volume_leaves_exactly_one_entry_for_the_mount_point(host) -> None:
    """ad8d F3 / AC1. The runbook's own "restore onto a fresh volume" path triggers this.

    The old guard was ``grep -q "$uuid" /etc/fstab`` — the NEW uuid only — so a replacement
    appended a second line and left the previous volume's behind. ``nofail`` hides that:
    neither line errors and which volume wins becomes fstab-ORDER dependent, permanently.
    """
    mount_point = host / "mnt" / "gate-scratch"
    stale = f"UUID=c814b8a1-dba9-4a59-b84c-0a6d6b6b38b2 {mount_point} xfs defaults,nofail 0 2"
    (host / "etc" / "fstab").write_text(f"{ROOT_FSTAB_LINE}\n{stale}\n", encoding="utf-8")
    (host / "blkid" / "nvme1n1").write_text(
        "UUID=b1e23678-982c-4a83-b6ab-d5d144c1f57f\n", encoding="utf-8"
    )

    proc = _run(host, f'mount_ebs_volume "{GOOD_VOLUME}" "{mount_point}"')

    assert proc.returncode == 0, proc.stderr
    entries = _fstab_lines(host, mount_point)
    assert len(entries) == 1, f"{len(entries)} entries for one mount point: {entries}"
    assert "b1e23678-982c-4a83-b6ab-d5d144c1f57f" in entries[0]
    assert "c814b8a1" not in entries[0]
    fstab = (host / "etc" / "fstab").read_text(encoding="utf-8")
    assert ROOT_FSTAB_LINE in fstab, (
        "an unrelated fstab entry was destroyed — the host would not boot"
    )


def test_the_previous_fstab_is_backed_up_before_it_is_rewritten(host) -> None:
    """ad8d AC2. A bad edit must be recoverable from the host itself, with no network."""
    mount_point = host / "mnt" / "gate-scratch"
    before = f"{ROOT_FSTAB_LINE}\n"
    (host / "etc" / "fstab").write_text(before, encoding="utf-8")
    (host / "blkid" / "nvme1n1").write_text("UUID=b1e23678\n", encoding="utf-8")

    _run(host, f'mount_ebs_volume "{GOOD_VOLUME}" "{mount_point}"')

    backup = host / "etc" / "fstab.rebar-bak"
    assert backup.exists(), "no backup of the pre-edit fstab was kept"
    assert backup.read_text(encoding="utf-8") == before


def test_rewriting_an_unchanged_volume_is_idempotent(host) -> None:
    """Re-running user_data.sh (the runbook tells operators to) must not grow fstab."""
    mount_point = host / "mnt" / "gate-scratch"
    (host / "etc" / "fstab").write_text(f"{ROOT_FSTAB_LINE}\n", encoding="utf-8")
    (host / "blkid" / "nvme1n1").write_text("UUID=b1e23678\n", encoding="utf-8")
    call = f'mount_ebs_volume "{GOOD_VOLUME}" "{mount_point}"'

    _run(host, call)
    _run(host, call)

    assert len(_fstab_lines(host, mount_point)) == 1


def test_removing_the_stale_entry_drop_restores_the_accumulation(host) -> None:
    """ad8d F3, seeded. Without the drop, the replacement scenario grows a second entry."""
    mount_point = host / "mnt" / "gate-scratch"
    stale = f"UUID=c814b8a1-dba9-4a59-b84c-0a6d6b6b38b2 {mount_point} xfs defaults,nofail 0 2"
    (host / "etc" / "fstab").write_text(f"{ROOT_FSTAB_LINE}\n{stale}\n", encoding="utf-8")
    (host / "blkid" / "nvme1n1").write_text("UUID=b1e23678\n", encoding="utf-8")

    _run(
        host,
        f'mount_ebs_volume "{GOOD_VOLUME}" "{mount_point}"',
        drop_lines=("DROP-STALE-FSTAB-ENTRY",),
    )

    entries = _fstab_lines(host, mount_point)
    assert len(entries) == 2, (
        "the seeded defect must reproduce the reported accumulation (two entries for one "
        f"mount point), got {entries}"
    )


# ── bug ad8d F5: both resolution branches answer in the same canonical form ───────────


def test_both_resolution_branches_return_the_same_string_for_the_same_device(host) -> None:
    """ad8d F5 / AC4. The nvme-cli path returned the glob path, the by-id path the realpath.

    Same device, two spellings, so a log line — or any future device COMPARISON — depended on
    which branch happened to fire. The reported evidence was ``/dev/nvme2n1`` from one branch
    and ``/dev/loop2`` from the other, so the fixture reproduces that shape: the node the glob
    finds is itself a link to the underlying device. Without this the two branches happen to
    agree and the test has no teeth.
    """
    underlying = host / "dev" / "loop2"
    underlying.write_bytes(b"\0" * 512)
    node = host / "dev" / "nvme1n1"
    node.unlink()
    node.symlink_to(underlying)

    via_nvme = _run(host, f'resolve_ebs_device "{GOOD_VOLUME}"')
    via_by_id = _run(host, f'resolve_ebs_device "{GOOD_VOLUME}"', broken_nvme=True)

    assert via_nvme.returncode == 0 and via_by_id.returncode == 0
    assert via_nvme.stdout == via_by_id.stdout, (
        "the two resolution branches disagree on how to spell the same device: "
        f"{via_nvme.stdout.strip()!r} vs {via_by_id.stdout.strip()!r}"
    )


# ── bug ad8d F4: the mount point is TIGHTENED before the markers are written ─────────

#: The marker-writing block of ``user_data.sh``, bounded by anchors that do NOT depend on the
#: order of the three statements inside it — so the extraction still finds the block when the
#: order is wrong, which is the case the test has to be able to observe.
_MARKER_BLOCK_START = "# The two marker files"
_MARKER_BLOCK_END = 'echo "Gate scratch mounted'


def _marker_block() -> str:
    src = USER_DATA.read_text(encoding="utf-8")
    start = src.index(_MARKER_BLOCK_START)
    end = src.index(_MARKER_BLOCK_END, start)
    return src[start:end]


def test_the_markers_are_never_written_into_a_world_readable_mount_point(host) -> None:
    """ad8d F4 / AC3. Asserted BEHAVIOURALLY: the mode observed AT THE MOMENT of each write.

    An earlier version of this test compared ``str.index`` of literal source fragments, which
    is a change-detector: it breaks on any behaviour-preserving edit and would pass on a text
    that reads right but does not run that way. The property that actually matters is
    observable — whether the marker files are created while the directory is still
    world-readable — so it is observed, by stubbing ``touch`` to record the mount point's
    permission bits as it finds them.

    The mount point starts 0755 (what ``mkdir -p`` leaves under the ambient umask), so the two
    orderings are distinguishable: tighten-then-write records ``drwx------`` for both markers,
    write-then-tighten records ``drwxr-xr-x`` for at least the first.
    """
    mount_point = host / "mnt" / "gate-scratch"
    mount_point.mkdir(parents=True)
    mount_point.chmod(0o755)
    modes_log = host / "modes.log"

    # `ls -ld | cut` rather than `stat`: the stat(1) flags for permission bits differ between
    # macOS and GNU coreutils, and this oracle runs on both.
    _stub(
        host / "bin" / "touch",
        f'ls -ld "$GATE_SCRATCH_MOUNT" | cut -c1-10 >> {modes_log}\n: > "$1"\n',
    )

    script = f'export GATE_SCRATCH_MOUNT="{mount_point}"\n' + _marker_block()
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=subprocess_env(PATH=f"{host}/bin:{os.environ['PATH']}"),
        timeout=60,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    observed = modes_log.read_text(encoding="utf-8").split()
    assert len(observed) == 2, f"expected both markers to be written, saw {observed}"
    assert all(m == "drwx------" for m in observed), (
        "a marker was written into the mount point while it was still world-readable "
        f"(modes observed at write time: {observed})"
    )


def test_removing_the_chmod_lets_the_markers_land_in_a_world_readable_directory(host) -> None:
    """ad8d F4, seeded. Without the tighten, the writes happen at 0755 — observably."""
    mount_point = host / "mnt" / "gate-scratch"
    mount_point.mkdir(parents=True)
    mount_point.chmod(0o755)
    modes_log = host / "modes.log"
    _stub(
        host / "bin" / "touch",
        f'ls -ld "$GATE_SCRATCH_MOUNT" | cut -c1-10 >> {modes_log}\n: > "$1"\n',
    )

    block = _marker_block()
    seeded = "\n".join(ln for ln in block.splitlines() if "chmod 0700" not in ln)
    assert seeded != block, "seed matched nothing: the chmod line moved or was renamed"

    subprocess.run(
        ["bash", "-c", f'export GATE_SCRATCH_MOUNT="{mount_point}"\n' + seeded],
        capture_output=True,
        text=True,
        env=subprocess_env(PATH=f"{host}/bin:{os.environ['PATH']}"),
        timeout=60,
        check=False,
    )

    observed = modes_log.read_text(encoding="utf-8").split()
    assert observed and all(m == "drwxr-xr-x" for m in observed), (
        "with the chmod removed the markers should be written into a world-readable "
        f"directory — if they are not, the test above proves nothing, got {observed}"
    )


# ── bug 9c93 AC4: an operator-invokable running-host check ───────────────────────────

CHECK_MOUNTS = REPO_ROOT / "infra" / "scripts" / "check-mounts.sh"


def test_the_running_host_check_exists_and_is_executable() -> None:
    """9c93 AC4. user_data.sh runs at FIRST BOOT only; every later boot has no witness."""
    assert CHECK_MOUNTS.exists(), "no running-host mount check for an operator to invoke"
    assert os.access(CHECK_MOUNTS, os.X_OK), "the check is not executable"


def test_the_running_host_check_names_both_volumes_by_default() -> None:
    """AC4 says BOTH volumes. It also pins the mount points against terraform's defaults.

    ``var.gate_scratch_mount``'s own documentation says four things must agree on that path;
    this check is a fifth consumer, so its default is pinned here rather than left to drift.
    """
    src = CHECK_MOUNTS.read_text(encoding="utf-8")
    assert "/var/gerrit" in src
    assert "/var/lib/rebar/gate-scratch" in src

    tf_default = (REPO_ROOT / "infra" / "terraform" / "variables.tf").read_text(encoding="utf-8")
    assert '"/var/lib/rebar/gate-scratch"' in tf_default


def test_the_running_host_check_fails_on_a_plain_directory(tmp_path: Path) -> None:
    """The dangerous state: the directory EXISTS, so every consumer looks healthy — on root.

    A check that only noticed a missing directory would pass on exactly the host this ticket
    is about, so the plain-directory case is the one worth executing.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _stub(fake_bin / "mountpoint", "exit 1\n")  # nothing is a mount point
    plain = tmp_path / "var" / "gerrit"
    plain.mkdir(parents=True)

    proc = subprocess.run(
        ["bash", str(CHECK_MOUNTS), str(plain)],
        capture_output=True,
        text=True,
        env=subprocess_env(PATH=f"{fake_bin}:{os.environ['PATH']}"),
        timeout=60,
        check=False,
    )

    assert proc.returncode != 0, "the check passed on a plain directory on the root filesystem"
    assert "ROOT filesystem" in proc.stderr, proc.stderr


def test_the_running_host_check_passes_when_the_volume_is_mounted(tmp_path: Path) -> None:
    """Control: the check must not simply always fail."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _stub(fake_bin / "mountpoint", "exit 0\n")
    _stub(fake_bin / "findmnt", "echo /dev/nvme1n1\n")
    mounted = tmp_path / "var" / "gerrit"
    mounted.mkdir(parents=True)

    proc = subprocess.run(
        ["bash", str(CHECK_MOUNTS), str(mounted)],
        capture_output=True,
        text=True,
        env=subprocess_env(PATH=f"{fake_bin}:{os.environ['PATH']}"),
        timeout=60,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr


# ── the portability fallbacks in check-mounts.sh actually EXECUTE ────────────────────
#
# Every test above stubs `mountpoint`, so the `/proc/mounts` branches that exist for a host
# WITHOUT util-linux were shipped unexecuted. A fallback nobody has ever run is a fallback that
# does not work: it is reached only on the unusual host, mid-incident, which is the worst
# moment to discover a typo. These drive the two branches directly, using the same technique as
# the rest of this module — the functions are extracted from the shipped script verbatim and
# only their one absolute path root is re-homed.


def _check_mounts_functions(proc_mounts: Path) -> str:
    """``is_mounted`` + ``describe_source``, verbatim, reading ``proc_mounts``."""
    src = CHECK_MOUNTS.read_text(encoding="utf-8")
    start = src.index("is_mounted() {")
    end = src.index("\nif [ ", start)
    body = src[start:end]
    assert "/proc/mounts" in body, "harness is stale: the /proc/mounts fallback is gone"
    body = body.replace("/proc/mounts", str(proc_mounts))
    # Make the util-linux probes miss, which is the whole condition these branches exist for.
    shim = (
        'command() { case "$2" in mountpoint|findmnt) return 1 ;; esac; builtin command "$@"; }\n'
    )
    return shim + body


def _run_fallback(tmp_path: Path, mounts_text: str, call: str):
    proc_mounts = tmp_path / "proc_mounts"
    proc_mounts.write_text(mounts_text, encoding="utf-8")
    script = _check_mounts_functions(proc_mounts) + "\n" + call + "\n"
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=subprocess_env(PATH=os.environ["PATH"]),
        timeout=60,
        check=False,
    )


_PROC_MOUNTS = "/dev/nvme0n1p1 / xfs rw,noatime 0 0\n/dev/nvme1n1 /var/gerrit xfs rw,noatime 0 0\n"


def test_the_is_mounted_fallback_finds_a_mounted_path_without_util_linux(tmp_path: Path) -> None:
    proc = _run_fallback(tmp_path, _PROC_MOUNTS, "is_mounted /var/gerrit")
    assert proc.returncode == 0, proc.stderr


def test_the_is_mounted_fallback_rejects_an_unmounted_path(tmp_path: Path) -> None:
    """The load-bearing direction: it must not report a plain directory as mounted."""
    proc = _run_fallback(tmp_path, _PROC_MOUNTS, "is_mounted /var/lib/rebar/gate-scratch")
    assert proc.returncode != 0, "the fallback called an UNMOUNTED path mounted"


def test_the_is_mounted_fallback_does_not_match_on_a_substring(tmp_path: Path) -> None:
    """``/var/gerrit-old`` must not satisfy a check for ``/var/gerrit``."""
    proc = _run_fallback(tmp_path, _PROC_MOUNTS, "is_mounted /var/ger")
    assert proc.returncode != 0


def test_the_describe_source_fallback_reports_the_device(tmp_path: Path) -> None:
    proc = _run_fallback(tmp_path, _PROC_MOUNTS, "describe_source /var/gerrit")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "/dev/nvme1n1"


def test_the_describe_source_fallback_prints_nothing_for_an_unknown_mount(tmp_path: Path) -> None:
    """This is the empty-source case the OK line has to be able to survive."""
    proc = _run_fallback(tmp_path, _PROC_MOUNTS, "describe_source /var/lib/rebar/gate-scratch")
    assert proc.stdout.strip() == ""


def test_the_ok_line_says_so_when_the_source_cannot_be_determined(tmp_path: Path) -> None:
    """An ``OK <mp> <- `` line with a blank source reads as a partial read, not a pass.

    A check whose success line is indistinguishable from a half-answer is a small instance of
    exactly the class this ticket is about, so the script says which of the two happened.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _stub(fake_bin / "mountpoint", "exit 0\n")
    _stub(fake_bin / "findmnt", "exit 1\n")  # an unusual mount: findmnt has no answer
    mounted = tmp_path / "var" / "gerrit"
    mounted.mkdir(parents=True)

    proc = subprocess.run(
        ["bash", str(CHECK_MOUNTS), str(mounted)],
        capture_output=True,
        text=True,
        env=subprocess_env(PATH=f"{fake_bin}:{os.environ['PATH']}"),
        timeout=60,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "source could not be determined" in proc.stdout, proc.stdout
    assert "<- \n" not in proc.stdout, f"blank-source OK line: {proc.stdout!r}"


def test_a_mount_point_containing_whitespace_is_one_target_not_two(tmp_path: Path) -> None:
    """A mount point is a PATH. Word-splitting one turned it into bogus targets.

    The old `targets="$*"` + unquoted `for` split `/var/my volume` into `/var/my` and `volume`,
    so the check reported on two directories that do not exist while never checking the real
    one — a check that fails for the wrong reason, which is worse than no check.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _stub(fake_bin / "mountpoint", "exit 0\n")
    _stub(fake_bin / "findmnt", "echo /dev/nvme1n1\n")
    spaced = tmp_path / "var" / "my volume"
    spaced.mkdir(parents=True)

    proc = subprocess.run(
        ["bash", str(CHECK_MOUNTS), str(spaced)],
        capture_output=True,
        text=True,
        env=subprocess_env(PATH=f"{fake_bin}:{os.environ['PATH']}"),
        timeout=60,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert str(spaced) in proc.stdout, proc.stdout
    assert proc.stdout.count("OK") == 1, f"one target became several: {proc.stdout!r}"


def test_is_mounted_says_it_cannot_tell_rather_than_claiming_not_mounted(tmp_path: Path) -> None:
    """The third outcome. With no ``mountpoint(1)`` AND no readable ``/proc/mounts``, the
    honest answer is "cannot determine", not "this is a plain directory on root".

    Reporting an unverified state as fact is precisely the failure mode this check exists to
    catch, so the check must not commit it itself. Exit 2 is that third state; the caller
    prints a distinct message for it.
    """
    absent = tmp_path / "no-such-proc-mounts"
    assert not absent.exists()
    script = _check_mounts_functions(absent) + "\nis_mounted /var/gerrit\n"

    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=subprocess_env(PATH=os.environ["PATH"]),
        timeout=60,
        check=False,
    )

    assert proc.returncode == 2, (
        "an undeterminable mount state must be its own outcome, not silently folded into "
        f'"not mounted" (exit 1), got {proc.returncode}'
    )
