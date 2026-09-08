"""Replication key rotation must reload Gerrit's in-memory SSH key before retirement."""

from pathlib import Path

SETUP_REPLICATION = (
    Path(__file__).resolve().parents[2] / "infra" / "gerrit" / "setup-replication.sh"
)


def test_deploy_key_rotation_lifecycle_restarts_before_verification_and_retirement() -> None:
    text = SETUP_REPLICATION.read_text()
    lifecycle = text.split("-- DEPLOY-KEY ROTATION LIFECYCLE --", 1)[1]

    restart_index = lifecycle.index("Restart Gerrit")
    verify_index = lifecycle.index("Verify replication")
    retire_index = lifecycle.index("REMOVE the OLD deploy key")

    assert restart_index < verify_index < retire_index
    assert "JGit caches SSH sessions/keys" in lifecycle


def test_setup_replication_warns_key_materialization_also_needs_gerrit_restart() -> None:
    text = SETUP_REPLICATION.read_text()

    assert "deploy key was re-materialised" in text
    assert "Gerrit may keep using a cached SSH key until restart" in text
