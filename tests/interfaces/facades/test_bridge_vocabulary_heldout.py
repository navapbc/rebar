"""Held-out oracle for canonical bridge spellings and retired compatibility aliases."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from _subprocess_env import subprocess_env

from rebar._cli import main

pytestmark = pytest.mark.unit


def _invoke(
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> tuple[int, str, str]:
    code = main(argv)
    streams = capsys.readouterr()
    return code, streams.out, streams.err


@pytest.mark.parametrize("exit_code", [0, 3])
@pytest.mark.parametrize(
    ("canonical", "patch_target"),
    [
        (["bridge", "fsck"], "rebar._engine_support.bridge_fsck.main"),
        (["bridge", "check-access"], "rebar._cli._bridge_probe"),
    ],
)
def test_canonical_bridge_commands_preserve_streams_arguments_and_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    canonical: list[str],
    patch_target: str,
    exit_code: int,
) -> None:
    """Canonical names route options through the existing implementation boundary."""
    calls: list[list[str]] = []

    def fake(argv: list[str], **_kwargs: object) -> int:
        calls.append(argv)
        print("shared stdout")
        print("shared stderr", file=__import__("sys").stderr)
        return exit_code

    monkeypatch.setattr(patch_target, fake)
    # The dispatch contract, not first-time tracker provisioning, is under test.
    monkeypatch.setattr("rebar.config.tracker_dir_override", lambda: "/tmp/bridge-test-tracker")
    options = ["--representative", "value"]

    new_result = _invoke(capsys, [*canonical, *options])

    assert new_result == (exit_code, "shared stdout\n", "shared stderr\n")
    assert calls == [options]


@pytest.mark.parametrize("exit_code", [0, 4])
def test_canonical_setup_uses_the_wizard_prog(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exit_code: int,
) -> None:
    """Normal setup behavior keeps the canonical argparse program name."""
    calls: list[tuple[list[str], str]] = []

    def fake(argv: list[str], *, prog: str = "rebar bridge setup") -> int:
        calls.append((argv, prog))
        print("shared wizard")
        return exit_code

    monkeypatch.setattr("rebar._cli._jira_onboard.jira_onboard", fake)
    options = ["--project", "OPS", "--no-validate"]

    canonical = _invoke(capsys, ["bridge", "setup", *options])

    assert canonical == (exit_code, "shared wizard\n", "")
    assert calls == [(options, "rebar bridge setup")]


def test_setup_final_validation_preserves_the_persisted_settings_overlay(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical setup still validates last with the just-written Jira coordinates."""
    from pathlib import Path

    from rebar import config

    root = Path(tmp_path)
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(config, "repo_root", lambda *_args: root)
    monkeypatch.delenv("JIRA_URL", raising=False)
    monkeypatch.delenv("JIRA_USER", raising=False)
    monkeypatch.delenv("JIRA_PROJECT", raising=False)
    monkeypatch.setenv("JIRA_API_TOKEN", "secret")
    config.reset_config_cache()
    captured: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_probe(argv: list[str], *, extra_env: dict[str, str] | None = None) -> int:
        captured.append((argv, extra_env))
        return 0

    monkeypatch.setattr("rebar._cli._bridge_probe", fake_probe)

    code = main(
        [
            "bridge",
            "setup",
            "--url",
            "https://jira.example",
            "--user",
            "operator@example.com",
            "--project",
            "OPS",
        ]
    )

    assert code == 0
    assert captured == [
        (
            [],
            {
                "JIRA_URL": "https://jira.example",
                "JIRA_USER": "operator@example.com",
                "JIRA_PROJECT": "OPS",
            },
        )
    ]
    assert (root / "rebar.toml").is_file()


@pytest.mark.parametrize(
    "option", ["--no-validate", "--reset", "--yes", "--url", "--user", "--project"]
)
def test_setup_help_exposes_the_option_set(option: str, cli_runner: Callable) -> None:
    """The canonical setup spelling retains the complete onboarding option surface."""
    canonical = cli_runner("bridge", "setup", "--help")

    assert canonical.returncode == 0
    assert canonical.stdout.lower().startswith("usage: rebar bridge setup")
    assert option in canonical.stdout


def test_check_access_is_a_command_not_an_fsck_flag(cli_runner: Callable) -> None:
    """The mutating capability round-trip is not disguised as an fsck option."""
    command = cli_runner("bridge", "check-access", "--help")
    flag = cli_runner("bridge", "fsck", "--check-access")

    assert command.returncode == 0
    assert flag.returncode != 0
    assert "check-access" not in flag.stdout


@pytest.fixture
def cli_runner(rebar_repo):
    """Run the actual module entrypoint with credentials removed."""
    import subprocess
    import sys

    def run(*args: str):
        env = subprocess_env()
        for key in ("JIRA_URL", "JIRA_USER", "JIRA_PROJECT", "JIRA_API_TOKEN"):
            env.pop(key, None)
        return subprocess.run(
            [sys.executable, "-m", "rebar.cli", *args],
            cwd=rebar_repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    return run
