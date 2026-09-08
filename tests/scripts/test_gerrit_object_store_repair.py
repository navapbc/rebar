"""Contracts for Gerrit object-store repair/prevention (pussycat)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_COMPOSE_UP = _REPO / "infra" / "scripts" / "compose-up.sh"
_AUTODEPLOY = _REPO / "infra" / "scripts" / "autodeploy.sh"
_JGIT_MATERIALIZER = _REPO / "infra" / "scripts" / "materialize-gerrit-jgit-config.sh"
_JGIT_CONFIG = _REPO / "infra" / "compose" / "jgit.config"
_COMPOSE = _REPO / "infra" / "compose" / "docker-compose.yml"
_RUNBOOK = _REPO / "infra" / "runbooks" / "gerrit-object-store-repair.md"


def test_jgit_config_disables_receive_autogc_in_versioned_infra() -> None:
    assert _JGIT_CONFIG.is_file(), "Gerrit jgit.config must be committed, not host-only"
    value = subprocess.check_output(
        ["git", "config", "--file", str(_JGIT_CONFIG), "--get", "receive.autogc"],
        text=True,
    ).strip()
    assert value == "false"


def test_jgit_materializer_seeds_and_validates_the_receive_section(tmp_path: Path) -> None:
    site = tmp_path / "site"
    subprocess.run(
        [
            "bash",
            str(_JGIT_MATERIALIZER),
            str(_REPO),
            str(site),
            f"{os.getuid()}:{os.getgid()}",
        ],
        check=True,
    )
    seeded = site / "etc" / "jgit.config"
    value = subprocess.check_output(
        ["git", "config", "--file", str(seeded), "--get", "receive.autogc"],
        text=True,
    ).strip()
    assert value == "false"

    bad_root = tmp_path / "bad-root"
    (bad_root / "infra" / "compose").mkdir(parents=True)
    (bad_root / "infra" / "compose" / "jgit.config").write_text("[core]\n\tautogc = false\n")
    result = subprocess.run(
        [
            "bash",
            str(_JGIT_MATERIALIZER),
            str(bad_root),
            str(tmp_path / "bad-site"),
            f"{os.getuid()}:{os.getgid()}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "receive.autogc must be false" in result.stderr


def test_compose_mounts_and_seeds_jgit_config() -> None:
    doc = yaml.safe_load(_COMPOSE.read_text())
    volumes = doc["services"]["gerrit"]["volumes"]
    assert "gerrit_etc:/var/gerrit/etc" in volumes

    text = _COMPOSE_UP.read_text()
    assert "materialize-gerrit-jgit-config.sh" in text
    assert '"${SITE_HOST_DIR}"' in text


def test_autodeploy_flags_jgit_config_for_manual_apply() -> None:
    text = _AUTODEPLOY.read_text()
    assert "infra/compose/jgit.config" in text
    assert "infra/scripts/materialize-gerrit-jgit-config.sh" in text


def test_repair_runbook_is_backup_first_and_connectivity_verified() -> None:
    text = _RUNBOOK.read_text()
    assert "create-snapshot" in text
    assert "git fetch" in text
    assert "--prune" not in text
    assert "refs/remotes/github/main" in text
    assert "chown -R 1000:1000" in text
    assert "fsck --full --connectivity-only --no-dangling --strict" in text
    assert "refs/changes" in text
    assert "cat-file -t" in text
