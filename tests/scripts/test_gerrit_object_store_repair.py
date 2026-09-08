"""Contracts for Gerrit object-store repair/prevention (pussycat)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_COMPOSE_UP = _REPO / "infra" / "scripts" / "compose-up.sh"
_AUTODEPLOY = _REPO / "infra" / "scripts" / "autodeploy.sh"
_JGIT_CONFIG = _REPO / "infra" / "compose" / "jgit.config"
_COMPOSE = _REPO / "infra" / "compose" / "docker-compose.yml"
_RUNBOOK = _REPO / "infra" / "runbooks" / "gerrit-object-store-repair.md"


def test_jgit_config_disables_receive_autogc_in_versioned_infra() -> None:
    assert _JGIT_CONFIG.is_file(), "Gerrit jgit.config must be committed, not host-only"
    text = _JGIT_CONFIG.read_text()
    assert re.search(r"(?ms)^\[receive\]\s+autogc\s*=\s*false\s*$", text)


def test_compose_mounts_and_seeds_jgit_config() -> None:
    doc = yaml.safe_load(_COMPOSE.read_text())
    volumes = doc["services"]["gerrit"]["volumes"]
    assert "gerrit_etc:/var/gerrit/etc" in volumes

    text = _COMPOSE_UP.read_text()
    assert "infra/compose/jgit.config" in text
    assert "${SITE_HOST_DIR}/etc/jgit.config" in text
    assert "receive.autogc=false" in text


def test_autodeploy_flags_jgit_config_for_manual_apply() -> None:
    text = _AUTODEPLOY.read_text()
    assert "infra/compose/jgit.config" in text


def test_repair_runbook_is_backup_first_and_connectivity_verified() -> None:
    text = _RUNBOOK.read_text()
    assert "create-snapshot" in text
    assert "git fetch" in text
    assert "chown -R 1000:1000" in text
    assert "fsck --full --connectivity-only --no-dangling --strict" in text
    assert "refs/changes" in text
    assert "cat-file -t" in text
