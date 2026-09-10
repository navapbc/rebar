"""The shell-free Gerrit recovery runbook preserves memory-wedge triage.

Bug f39e-8ca9-30f2-44fc exists because the 2026-09-05 total outage could not
be proven after SSM died: the missing evidence was an off-box memory run-up
series, not another shell command. This guard keeps the runbook from drifting
back to disk-only recovery guidance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

RUNBOOK = (
    Path(__file__).resolve().parents[2] / "infra" / "runbooks" / "gerrit-host-wedged-ssm-lost.md"
)


def _runbook_text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def test_runbook_names_memory_runup_metrics_and_alarms() -> None:
    """A future no-SSM outage must be diagnosable from off-box memory evidence."""
    text = _runbook_text()

    for required in (
        "mem_available_percent",
        "mem_used_percent",
        "container_memory_rss_bytes",
        "mem_probe_ok",
        "container_stats_ok",
        "rebar-host-memory-low",
        "rebar-mem-signal-absent-while-probe-alive",
        "rebar-memory-probe-not-ok",
    ):
        assert required in text


def test_runbook_records_discriminators_for_non_memory_causes() -> None:
    """Memory exhaustion must be separated from CPU, EC2, disk, and EBS failures."""
    text = _runbook_text()

    for required in (
        "CPUUtilization",
        "StatusCheckFailed",
        "EBSIOBalance%",
        "EBSByteBalance%",
        "root_disk_used_percent",
    ):
        assert required in text


def test_runbook_records_networkin_detector_decision_and_spike_status() -> None:
    """The surviving hypervisor signal is a corroborator, not the root alarm."""
    text = _runbook_text()

    assert "NetworkIn COLLAPSES" in text
    assert "do not page on NetworkIn collapse alone" in text
    assert "23:13 NetworkIn spike remains unattributed" in text
