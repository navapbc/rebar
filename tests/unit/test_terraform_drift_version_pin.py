"""Regression guard for bug d227-7fa6: the Terraform Drift workflow must provision a
Terraform CLI that SATISFIES the config's own ``required_version`` floor.

The drift check (``.github/workflows/terraform-drift.yml``) pins a Terraform version via
``hashicorp/setup-terraform``. ``infra/terraform/versions.tf`` declares a ``required_version``
floor (``>= 1.11``, deliberately raised for write-only SSM secret arguments — ADR 0105). If the
pinned CLI is BELOW that floor, ``terraform init``/``validate`` hard-fails with "Unsupported
Terraform Core version" BEFORE the drift ``plan`` step ever runs, so drift detection is silently
blind. Bug d227 was exactly this: the floor moved to ``>= 1.11`` (commit d14c410) while the
workflow kept pinning ``1.10.5``.

Pure-Python + offline: no terraform binary, no AWS, no CI provider — portable to any CI or none.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from packaging.version import Version

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_VERSIONS = _REPO / "infra" / "terraform" / "versions.tf"
_WORKFLOW = _REPO / ".github" / "workflows" / "terraform-drift.yml"


def _required_version_floor() -> Version:
    """The minimum Terraform core version required by infra/terraform/versions.tf."""
    text = _VERSIONS.read_text()
    m = re.search(r'required_version\s*=\s*"[^"]*?>=\s*([0-9]+(?:\.[0-9]+){1,2})', text)
    assert m is not None, 'no `required_version = ">= X"` floor found in versions.tf'
    return Version(m.group(1))


def _drift_workflow_pins() -> list[Version]:
    """Every terraform_version pinned in the Terraform Drift workflow's setup-terraform steps."""
    doc = yaml.safe_load(_WORKFLOW.read_text())
    pins: list[Version] = []
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            uses = str(step.get("uses") or "")
            if "hashicorp/setup-terraform" in uses:
                pin = (step.get("with") or {}).get("terraform_version")
                assert pin is not None, "setup-terraform step lacks an explicit terraform_version"
                pins.append(Version(str(pin)))
    return pins


def test_drift_workflow_pins_a_terraform() -> None:
    """The drift workflow provisions Terraform via setup-terraform with an explicit pin."""
    assert _drift_workflow_pins(), "no hashicorp/setup-terraform pin found in terraform-drift.yml"


def test_drift_pin_satisfies_required_version_floor() -> None:
    """Every pinned Terraform version must satisfy versions.tf's required_version floor, or
    `terraform init` fails with 'Unsupported Terraform Core version' before drift plan runs."""
    floor = _required_version_floor()
    for pin in _drift_workflow_pins():
        assert pin >= floor, (
            f"terraform-drift.yml pins Terraform {pin}, below versions.tf required_version "
            f">= {floor}; init/validate will fail before the drift plan (bug d227-7fa6)"
        )
