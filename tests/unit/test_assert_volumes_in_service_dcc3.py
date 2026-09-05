"""Oracle for the attached-but-not-in-service assertion [rebar:dcc3-75ee-26ce-4840].

The bug: the gate-scratch EBS volume was attached by a `terraform apply` AFTER first boot, so
`user_data.sh` (cloud-init, first boot only) never formatted or mounted it. Terraform recorded a
healthy attachment and converged; the host ran gate scratch on the root filesystem for days.

Every test here runs the real decision logic offline -- no AWS, no SSM, no terraform. The fixture
values are the REAL ones measured on `i-00880b2c7f13527c5` on 2026-09-05, so the headline test
reproduces the actual production state rather than an invented one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "assert_volumes_in_service.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from assert_volumes_in_service import (  # noqa: E402
    IN_SERVICE,
    NOT_IN_SERVICE,
    UNKNOWN,
    attachments_from_plan,
    classify,
    parse_host_mounts,
)

#: `lsblk -P -o NAME,SERIAL,MOUNTPOINT` as the production host reports it. Two shapes matter:
#: the gate-scratch volume is attached with NO mount, and the ROOT volume carries its serial on
#: the DEVICE line while the mount lives on its PARTITION.
REAL_HOST_REPORT = """NAME="nvme1n1" SERIAL="vol06fa2e77a9dd97527" MOUNTPOINT="/var/gerrit"
NAME="nvme0n1" SERIAL="vol0270fcf13709cf472" MOUNTPOINT=""
NAME="nvme0n1p1" SERIAL="" MOUNTPOINT="/"
NAME="nvme0n1p128" SERIAL="" MOUNTPOINT="/boot/efi"
NAME="nvme2n1" SERIAL="vol06780b8557d1416b7" MOUNTPOINT=""
"""

ROOT_VOLUME = "vol-0270fcf13709cf472"
DATA_VOLUME = "vol-06fa2e77a9dd97527"
SCRATCH_VOLUME = "vol-06780b8557d1416b7"


def _plan(*attachments: tuple[str, str]) -> dict:
    return {
        "planned_values": {
            "root_module": {
                "resources": [
                    {
                        "type": "aws_volume_attachment",
                        "values": {"volume_id": vol, "device_name": dev},
                    }
                    for vol, dev in attachments
                ]
            }
        }
    }


def _run(plan: dict, report: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    plan_file, report_file = tmp_path / "plan.json", tmp_path / "host.txt"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    report_file.write_text(report, encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--plan-json",
            str(plan_file),
            "--instance-id",
            "i-00880b2c7f13527c5",
            "--host-report",
            str(report_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


# ── the production failure, reproduced ────────────────────────────────────────────────────


def test_the_real_production_state_fails_the_assertion(tmp_path: Path) -> None:
    """RED: this is the state that went unnoticed for days. It must be a loud failure."""
    plan = _plan((DATA_VOLUME, "/dev/sdf"), (SCRATCH_VOLUME, "/dev/sdg"))
    result = _run(plan, REAL_HOST_REPORT, tmp_path)

    assert result.returncode == 1
    assert NOT_IN_SERVICE in result.stdout
    assert SCRATCH_VOLUME in result.stdout
    # ...and the healthy one is still reported as healthy, so the failure is specific.
    assert f"{IN_SERVICE:<15} {DATA_VOLUME}" in result.stdout
    assert "gate-scratch-mount.md" in result.stderr


def test_the_assertion_passes_once_the_volume_is_mounted(tmp_path: Path) -> None:
    """GREEN: the same plan against a host where the runbook has been executed."""
    fixed = REAL_HOST_REPORT.replace(
        'NAME="nvme2n1" SERIAL="vol06780b8557d1416b7" MOUNTPOINT=""',
        'NAME="nvme2n1" SERIAL="vol06780b8557d1416b7" MOUNTPOINT="/var/lib/rebar/gate-scratch"',
    )
    result = _run(_plan((DATA_VOLUME, "/dev/sdf"), (SCRATCH_VOLUME, "/dev/sdg")), fixed, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert NOT_IN_SERVICE not in result.stdout


# ── the join itself: the serial normalisation is the load-bearing detail ─────────────────


def test_the_volume_id_is_matched_against_the_dashless_nvme_serial() -> None:
    """An NVMe EBS device reports `vol0abc...`, terraform says `vol-0abc...`.

    A literal comparison would match NOTHING, so every volume would land in UNKNOWN. That
    still fails, so the script would look like it worked while asserting nothing real -- the
    exact vacuous-guard shape this project keeps finding. Asserted directly.
    """
    mounts = parse_host_mounts(
        'NAME="nvme2n1" SERIAL="vol06780b8557d1416b7" MOUNTPOINT="/var/lib/rebar/gate-scratch"'
    )
    assert mounts == {"vol06780b8557d1416b7": "/var/lib/rebar/gate-scratch"}

    [verdict] = classify([(SCRATCH_VOLUME, "/dev/sdg")], mounts)
    assert verdict.state == IN_SERVICE, "the dashed id failed to join the dashless serial"


def test_an_unreported_device_is_UNKNOWN_and_still_fails(tmp_path: Path) -> None:
    """'I could not tell' must never read as 'it is fine'.

    Three outcomes, not two -- the same discipline `infra/scripts/check-mounts.sh` applies.
    """
    [verdict] = classify([(SCRATCH_VOLUME, "/dev/sdg")], {})
    assert verdict.state == UNKNOWN
    assert not verdict.ok

    result = _run(_plan((SCRATCH_VOLUME, "/dev/sdg")), "", tmp_path)
    assert result.returncode == 1
    assert UNKNOWN in result.stdout


def test_an_empty_attachment_set_fails_rather_than_passing_vacuously(tmp_path: Path) -> None:
    """A plan with no attachments means the script asserted NOTHING. That is not a pass."""
    result = _run({"planned_values": {"root_module": {"resources": []}}}, "", tmp_path)
    assert result.returncode == 1
    assert "nothing asserted" in result.stderr


# ── plan parsing ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["planned_values", "values"])
def test_attachments_are_read_from_both_plan_and_state_shapes(key: str) -> None:
    """`terraform show -json <planfile>` yields planned_values; state yields values."""
    doc = {
        key: {
            "root_module": {
                "resources": [
                    {
                        "type": "aws_volume_attachment",
                        "values": {"volume_id": SCRATCH_VOLUME, "device_name": "/dev/sdg"},
                    },
                    {"type": "aws_instance", "values": {"id": "i-1"}},
                ]
            }
        }
    }
    assert attachments_from_plan(doc) == [(SCRATCH_VOLUME, "/dev/sdg")]


def test_a_volume_whose_id_is_unknown_at_plan_time_is_skipped() -> None:
    """A not-yet-created volume cannot be out of service; flagging it would be a false red."""
    doc = {
        "planned_values": {
            "root_module": {
                "resources": [
                    {
                        "type": "aws_volume_attachment",
                        "values": {"volume_id": None, "device_name": "/dev/sdh"},
                    }
                ]
            }
        }
    }
    assert attachments_from_plan(doc) == []


# ── defect seeding: prove the assertion is load-bearing ──────────────────────────────────


def test_only_a_confirmed_mount_counts_as_healthy() -> None:
    """`ok` must be true for exactly one of the three states.

    The two ways this guard could go vacuous are folding UNKNOWN into healthy (an unreachable
    host reads clean) or folding NOT-IN-SERVICE into healthy (the production bug itself).
    Pinning the healthy set to a single literal state closes both at once. Confirmed by
    perturbation: making `classify` treat an unmounted volume as in-service turns
    `test_the_real_production_state_fails_the_assertion` red, and dropping the dash
    normalisation turns three tests red.
    """
    healthy = {
        state
        for state in (IN_SERVICE, NOT_IN_SERVICE, UNKNOWN)
        for verdict in classify(
            [(SCRATCH_VOLUME, "/dev/sdg")],
            {"vol06780b8557d1416b7": "/mnt" if state == IN_SERVICE else None}
            if state != UNKNOWN
            else {},
        )
        if verdict.ok
    }
    assert healthy == {IN_SERVICE}


def test_a_volume_mounted_through_its_PARTITION_is_not_reported_unmounted() -> None:
    """The root volume carries the serial; its PARTITION carries the mount.

    `lsblk` puts SERIAL on the device line (`nvme0n1`) and MOUNTPOINT on the partition
    (`nvme0n1p1`), and a partition does NOT repeat its parent's serial. Reading only the
    device line therefore reports a perfectly healthy volume as NOT-IN-SERVICE. Found against
    the real host output, not reasoned about: the production `lsblk` shows `nvme0n1` with an
    empty MOUNTPOINT even though `/` is mounted from it.

    This matters the moment any root-like or partitioned volume becomes an
    `aws_volume_attachment` -- the assertion would fire on a healthy host, and a guard that
    cries wolf gets switched off.
    """
    mounts = parse_host_mounts(REAL_HOST_REPORT)
    assert mounts["vol0270fcf13709cf472"] == "/", "partition mount was not rolled up to parent"

    [verdict] = classify([(ROOT_VOLUME, "/dev/sda1")], mounts)
    assert verdict.state == IN_SERVICE
    # ...while the genuinely unmounted volume in the SAME report is still caught.
    [scratch] = classify([(SCRATCH_VOLUME, "/dev/sdg")], mounts)
    assert scratch.state == NOT_IN_SERVICE
