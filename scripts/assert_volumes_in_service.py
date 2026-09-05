#!/usr/bin/env python3
# mechanism-ok: ci_gate scripts/assert_volumes_in_service.py — bug dcc3-75ee-26ce-4840: a volume
# attached by `terraform apply` AFTER first boot is never mounted, because user_data.sh runs only
# at launch. Terraform knows it attached the volume; nothing compared that against whether the
# volume actually entered service, so a closed story sat provably not-in-effect for days.
"""Assert every EBS volume Terraform attaches is actually IN SERVICE on the host.

The gap this closes [rebar:dcc3-75ee-26ce-4840]. ``infra/terraform/user_data.sh`` is the only
thing that formats, mounts and marks a volume, and cloud-init runs it EXACTLY ONCE, at the
instance's first boot. So a volume attached by a later ``terraform apply`` is guaranteed to sit
raw and unmounted: Terraform records a healthy ``aws_volume_attachment``, the plan converges,
and the host silently keeps using the root filesystem. That is not hypothetical -- it is what
happened to the gate-scratch volume, which was attached, never formatted, and ran on root for
days while the story that delivered it was closed.

**What makes it visible.** Terraform holds one half of the answer (which volumes are attached to
which instance) and the host holds the other (which mount points are backed by a real
filesystem). Neither half is wrong on its own; nothing joined them. This script performs that
join and fails loudly, naming the volume and the mount point, so "delivered but not in effect"
becomes an actionable error at apply time rather than an alarm someone notices days later.

**Why not fix it on the host instead.** Two host-side alternatives were considered and rejected:

* *Auto-reconcile post-boot* -- have a periodic unit mount whatever is attached. Mounting an
  already-formatted volume is safe, but this failure's volume was RAW, and auto-``mkfs`` on a
  volume that merely looks blank is how a restored backup gets destroyed. The unsafe half is
  exactly the half that was needed, so automation cannot cover it.
* *Make rebar's gate admission refuse* -- ``rebar.llm.gate_admission.scratch_unavailable_detail``
  returns ``None`` when the DECLARATION marker is absent, which is this host's state, so the
  refusal never fires. Flipping that is not a small change: the marker pair is deliberately
  "opt-in by PROVISIONING rather than by rebar version" so that no laptop or CI runner starts
  failing gates on upgrade, and the module says so in as many words. Landing a flip while this
  host's volume is still unmounted would ALSO refuse every gate on the box. It is a real design
  question and it belongs in its own ticket, not smuggled into a bug fix.

**Portable by construction** (``project.portability``): plain Python plus whatever the caller
already uses to reach AWS. It is an operation-linked check -- run it after an apply, from a
runbook, or from any scheduler -- and it is not wired to any one CI provider. The decision logic
is a pure function so it is proven offline, with no AWS, by
``tests/unit/test_assert_volumes_in_service_dcc3.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: Terraform resource type that records "this volume is attached to this instance".
ATTACHMENT_TYPE = "aws_volume_attachment"

IN_SERVICE = "in-service"
NOT_IN_SERVICE = "NOT-IN-SERVICE"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Verdict:
    """One attached volume, and whether the host is actually using it."""

    volume_id: str
    device_name: str
    mount_point: str | None
    state: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.state == IN_SERVICE

    def render(self) -> str:
        where = self.mount_point or self.device_name
        return f"{self.state:<15} {self.volume_id}  {where}  -- {self.detail}"


def attachments_from_plan(plan: dict) -> list[tuple[str, str]]:
    """``(volume_id, device_name)`` for every ``aws_volume_attachment`` in a plan/state JSON.

    Reads ``planned_values`` when present (``terraform show -json <plan>``) and falls back to
    ``values`` (``terraform show -json`` of state). A volume whose id is still unknown at plan
    time is skipped: it does not exist yet, so it cannot be out of service.
    """
    root = (plan.get("planned_values") or plan.get("values") or {}).get("root_module") or {}
    found: list[tuple[str, str]] = []
    for resource in root.get("resources") or []:
        if resource.get("type") != ATTACHMENT_TYPE:
            continue
        values = resource.get("values") or {}
        volume_id, device = values.get("volume_id"), values.get("device_name")
        if volume_id and device:
            found.append((str(volume_id), str(device)))
    return sorted(set(found))


def parse_host_mounts(report: str) -> dict[str, str | None]:
    """Parse ``lsblk -P -o NAME,SERIAL,MOUNTPOINT`` pairs into ``serial -> mountpoint | None``.

    Two details are load-bearing, and both were found against the REAL host output rather than
    reasoned about:

    * **The serial is the volume id WITHOUT its dash.** An NVMe EBS device reports the id as
      ``vol<hex>`` where Terraform says ``vol-<hex>``, so the join normalises by stripping
      dashes. A literal comparison matches nothing, which would put every volume in
      ``UNKNOWN`` -- a guard that fails for the wrong reason and proves nothing.
    * **A PARTITION carries the mount, and does not repeat its parent's serial.** On the
      production host the root volume prints ``NAME="nvme0n1" SERIAL="vol0270..."
      MOUNTPOINT=""`` while ``NAME="nvme0n1p1" SERIAL="" MOUNTPOINT="/"`` holds the actual
      mount. Reading only the device line therefore reports a mounted volume as UNMOUNTED.
      Partition mounts are rolled up to the parent device by name prefix.

    ``-P`` (key="value" pairs) rather than ``-nr``: with raw output an empty SERIAL column
    collapses and a partition line becomes indistinguishable from a two-column device line.
    """
    devices: dict[str, str] = {}  # device name -> serial
    mounts: dict[str, str] = {}  # device name -> mountpoint
    for line in report.splitlines():
        fields = dict(re.findall(r'(\w+)="([^"]*)"', line))
        name = fields.get("NAME", "").strip()
        if not name:
            continue
        if fields.get("SERIAL", "").strip():
            devices[name] = fields["SERIAL"].strip().replace("-", "")
        if fields.get("MOUNTPOINT", "").strip():
            mounts[name] = fields["MOUNTPOINT"].strip()

    resolved: dict[str, str | None] = {}
    for name, serial in devices.items():
        mount = mounts.get(name)
        if mount is None:
            # Roll a partition's mount up to its parent device: nvme0n1p1 -> nvme0n1.
            mount = next(
                (m for dev, m in sorted(mounts.items()) if dev != name and dev.startswith(name)),
                None,
            )
        resolved[serial] = mount
    return resolved


def classify(attached: list[tuple[str, str]], host_mounts: dict[str, str | None]) -> list[Verdict]:
    """Join Terraform's attachment set to the host's mount table.

    Three outcomes, never two. ``UNKNOWN`` -- the host did not report this device at all --
    is kept distinct from ``NOT-IN-SERVICE`` and still FAILS, because "I could not tell" must
    not read as "it is fine". That conflation is the shape of the bug this script exists for.
    """
    verdicts: list[Verdict] = []
    for volume_id, device in attached:
        key = volume_id.replace("-", "")
        if key not in host_mounts:
            verdicts.append(
                Verdict(
                    volume_id,
                    device,
                    None,
                    UNKNOWN,
                    "the host did not report this device; mount state could not be determined",
                )
            )
        elif host_mounts[key] is None:
            verdicts.append(
                Verdict(
                    volume_id,
                    device,
                    None,
                    NOT_IN_SERVICE,
                    "attached but NOT MOUNTED -- consumers are silently using the root "
                    "filesystem; see infra/runbooks/gate-scratch-mount.md",
                )
            )
        else:
            verdicts.append(Verdict(volume_id, device, host_mounts[key], IN_SERVICE, "mounted"))
    return verdicts


#: What the host runs. One `NAME="..." SERIAL="..." MOUNTPOINT="..."` line per block device,
#: partitions included -- the rollup in :func:`parse_host_mounts` needs both.
HOST_PROBE = "lsblk -P -o NAME,SERIAL,MOUNTPOINT"


def probe_host(instance_id: str, region: str) -> str:
    """Run :data:`HOST_PROBE` on ``instance_id`` via SSM and return its stdout."""
    sent = json.loads(
        subprocess.run(
            [
                "aws",
                "ssm",
                "send-command",
                "--region",
                region,
                "--instance-ids",
                instance_id,
                "--document-name",
                "AWS-RunShellScript",
                "--parameters",
                json.dumps({"commands": [HOST_PROBE]}),
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    command_id = sent["Command"]["CommandId"]
    subprocess.run(
        [
            "aws",
            "ssm",
            "wait",
            "command-executed",
            "--region",
            region,
            "--command-id",
            command_id,
            "--instance-id",
            instance_id,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return subprocess.run(
        [
            "aws",
            "ssm",
            "get-command-invocation",
            "--region",
            region,
            "--command-id",
            command_id,
            "--instance-id",
            instance_id,
            "--query",
            "StandardOutputContent",
            "--output",
            "text",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--plan-json",
        type=Path,
        required=True,
        help="`terraform show -json` output (a plan file or current state).",
    )
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--host-report",
        type=Path,
        help="Read the host probe from this file instead of calling SSM.",
    )
    args = parser.parse_args(argv)

    attached = attachments_from_plan(json.loads(args.plan_json.read_text(encoding="utf-8")))
    if not attached:
        print(f"no {ATTACHMENT_TYPE} resources found -- nothing asserted", file=sys.stderr)
        return 1
    report = (
        args.host_report.read_text(encoding="utf-8")
        if args.host_report
        else probe_host(args.instance_id, args.region)
    )
    verdicts = classify(attached, parse_host_mounts(report))
    for verdict in verdicts:
        print(verdict.render())
    bad = [v for v in verdicts if not v.ok]
    if bad:
        print(
            f"\nFAIL: {len(bad)} of {len(verdicts)} attached volume(s) are not in service. "
            "Terraform believes they are provisioned; the host is not using them. This is the "
            "'delivered but not in effect' state -- a volume attached after first boot is never "
            "mounted, because user_data.sh runs only at launch. Remediate with "
            "infra/runbooks/gate-scratch-mount.md; do NOT re-apply and expect it to self-heal.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
