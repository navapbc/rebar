"""Task 830a: deterministic public-exposure detectors in the code-review security overlay.

Pins: the detectors register on the opengrep backend under the dedicated
`rebar.builtin.iac.public-exposure.` prefix (NOT the fail-closed security prefix); a real
opengrep/semgrep scan MATCHES the unambiguous public-exposure literals (0.0.0.0/0 ingress, ::/0,
public IP, internet-facing LB, non-loopback compose bind) and does NOT match the FP-guarded
negatives (private CIDR, loopback, internal LB); and the consumer keeps the verdict ADVISORY
(never auto-BLOCK) for this criterion.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PREFIX = "rebar.builtin.iac.public-exposure."

# opengrep runs via the semgrep fallback binary; skip the real-scan tests when neither is present.
_HAS_ENGINE = shutil.which("opengrep") is not None or shutil.which("semgrep") is not None

POSITIVE_TF = """
resource "aws_security_group" "bad_v4" {
  ingress {
    from_port   = 22
    to_port     = 22
    cidr_blocks = ["10.0.0.0/8", "0.0.0.0/0"]
  }
}
resource "aws_security_group_rule" "bad_v6" {
  type             = "ingress"
  ipv6_cidr_blocks = ["::/0"]
}
resource "aws_instance" "bad_pubip" {
  associate_public_ip_address = true
}
resource "aws_lb" "bad_lb" {
  internal = false
}
"""

POSITIVE_COMPOSE = """
services:
  bad:
    ports:
      - "0.0.0.0:8080:80"
"""

NEGATIVE_TF = """
resource "aws_security_group" "ok_private" {
  ingress {
    cidr_blocks = ["10.0.0.0/8", "192.168.0.0/16"]
  }
}
resource "aws_lb" "ok_internal" {
  internal = true
}
resource "aws_instance" "ok_nopubip" {
  associate_public_ip_address = false
}
"""

NEGATIVE_COMPOSE = """
services:
  ok_loopback:
    ports:
      - "127.0.0.1:5432:5432"
  ok_internal:
    ports:
      - "5432:5432"
"""


# ── registration ─────────────────────────────────────────────────────────────────────────
def test_public_exposure_detectors_register_on_opengrep():
    from rebar.grounding.detectors import BACKEND_OPENGREP, load_registry

    pe = [d for d in load_registry() if d.id.startswith(PREFIX)]
    assert len(pe) == 5
    assert all(d.backend == BACKEND_OPENGREP for d in pe)
    assert all(d.dimension == "has_iac" for d in pe)
    # The rule file exists and cites its tfsec/Checkov analog (alignment, not a bridge).
    body = Path(
        "src/rebar/grounding/detectors/builtin/security_iac_public_exposure.yaml"
    ).read_text()
    assert PREFIX in body
    assert "tfsec" in body and "CKV_AWS" in body


def test_public_exposure_is_a_dedicated_advisory_det_criterion():
    # Routed to its OWN criterion, NOT swept into the fail-closed `high-critical-security` prefix,
    # and ADVISORY (blocking_enabled False / fail_mode open) so it never auto-blocks.
    from rebar.llm.code_review import registry

    dm = registry.det_criteria()
    assert "public-exposure-without-auth" in dm
    assert dm["public-exposure-without-auth"]["fail_mode"] == "open"
    _threshold, blocking = registry.threshold_for(["public-exposure-without-auth"])
    assert blocking is False
    # detector ids route to the dedicated criterion, not high-critical-security.
    routed = registry.criterion_for_detector(f"{PREFIX}tf-ingress-open-ipv4", dm)
    assert routed == "public-exposure-without-auth"


# ── real scan: positive matches + negative FP guards ────────────────────────────────────────
@pytest.mark.skipif(not _HAS_ENGINE, reason="opengrep/semgrep not installed")
def test_positive_public_exposure_matches(tmp_path):
    from rebar.llm.code_review.detectors import run_detectors

    (tmp_path / "main.tf").write_text(POSITIVE_TF)
    (tmp_path / "docker-compose.yml").write_text(POSITIVE_COMPOSE)
    out = run_detectors(changed_files=["main.tf", "docker-compose.yml"], repo_root=str(tmp_path))
    pe = out.get("public-exposure-without-auth", {"matches": []})
    matched_rules = {m["detector_id"].split(".")[-1] for m in pe["matches"]}
    assert matched_rules == {
        "tf-ingress-open-ipv4",
        "tf-ingress-open-ipv6",
        "tf-associate-public-ip",
        "tf-public-lb",
        "compose-nonloopback-bind",
    }


@pytest.mark.skipif(not _HAS_ENGINE, reason="opengrep/semgrep not installed")
def test_negative_fp_guards_do_not_match(tmp_path):
    from rebar.llm.code_review.detectors import run_detectors

    (tmp_path / "main.tf").write_text(NEGATIVE_TF)
    (tmp_path / "docker-compose.yml").write_text(NEGATIVE_COMPOSE)
    out = run_detectors(changed_files=["main.tf", "docker-compose.yml"], repo_root=str(tmp_path))
    pe = out.get("public-exposure-without-auth", {"matches": []})
    # private CIDR, loopback, internal LB, public_ip=false → no public-exposure finding.
    assert pe["matches"] == []


@pytest.mark.skipif(not _HAS_ENGINE, reason="opengrep/semgrep not installed")
def test_public_exposure_match_stays_advisory(tmp_path, monkeypatch):
    # A match on this criterion must NOT force a BLOCK (advisory posture). Isolate the consumer to
    # only this criterion so the unrelated fail-closed security abstain (no python files here) does
    # not colour the assertion.
    from rebar.llm.code_review import detectors, registry

    only = {
        "public-exposure-without-auth": {
            "detector": {"id_prefix": PREFIX},
            "fail_mode": "open",
        }
    }
    monkeypatch.setattr(registry, "det_criteria", lambda: only)

    (tmp_path / "main.tf").write_text(POSITIVE_TF)
    verdict = {"verdict": "PASS"}
    detectors.apply_failclosed(verdict, changed_files=["main.tf"], repo_root=str(tmp_path))
    assert verdict["verdict"] == "PASS"  # advisory — a match never auto-blocks
    notes = verdict.get("coverage", {}).get("security_detectors", [])
    note = next(n for n in notes if n["criterion"] == "public-exposure-without-auth")
    assert note["reason"] == "detector-finding"
    assert note["blocking"] is False
    assert note["count"] >= 4
