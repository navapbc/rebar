"""Signal manual Gerrit apply when detect-only sources change.

Gerrit config and deploy-key materializer changes reach the host copy but require
operator application. The shipped autodeploy script must emit config_manual naming
the changed source without restarting Gerrit unattended.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

AUTODEPLOY = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "autodeploy.sh"
_DEPLOYED = "d" * 40
_TARGET = "e" * 40

# Detect-only Gerrit sources and the detail each manual-apply signal names.
_CONFIG_MANUAL_SOURCES = [
    ("infra/compose/gerrit.config", "gerrit.config"),
    ("infra/gerrit/materialize-deploy-key.sh", None),
]


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    p.chmod(0o755)


@pytest.fixture
def deploy_box(tmp_path: Path) -> dict[str, object]:
    """A fake box where main advanced; the changed source is injected per-test via CHANGED_PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    deploy_repo = tmp_path / "deploy"
    (deploy_repo / "infra" / "compose").mkdir(parents=True)
    (deploy_repo / "infra" / "gerrit").mkdir(parents=True)
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)  # so autodeploy skips the bootstrap clone
    (mirror / "infra" / "compose").mkdir(parents=True)
    (mirror / "infra" / "gerrit").mkdir(parents=True)

    # Seed deployed-sha so this is neither first-run nor up-to-date.
    (state / "deployed-sha").write_text(_DEPLOYED + "\n")

    # git stub: the diff whose `--` pathspec names $CHANGED_PATH reports THAT path changed;
    # every other manifest's diff names other files, returns empty, and its block is skipped.
    _stub(
        bin_dir,
        "git",
        f"""
        args=("$@"); sub=""
        for ((i=0; i<${{#args[@]}}; i++)); do
          case "${{args[i]}}" in -C) ((i++));; -*) ;; *) sub="${{args[i]}}"; break;; esac
        done
        case "$sub" in
          remote) echo "https://github.com/navapbc/rebar.git"; exit 0 ;;
          fetch)  exit 0 ;;
          rev-parse) echo "{_TARGET}"; exit 0 ;;
          checkout) exit 0 ;;
          show)
            case "$*" in
              *"{_DEPLOYED}:"*) printf '%s' "${{DEPLOYED_BODY-}}" ;;
              *)  [ -n "${{TARGET_MISSING-}}" ] && exit 128; printf '%s' "${{TARGET_BODY-}}" ;;
            esac
            exit 0 ;;
          diff)
            case "$*" in
              *"$CHANGED_PATH"*) echo "$CHANGED_PATH"; exit 0 ;;
            esac
            exit 0 ;;
          *) exit 0 ;;
        esac
        """,
    )
    # flock/timeout are GNU/Linux-only and absent on macOS runners; without these stubs the
    # whole deploy short-circuits and the config block never runs.
    _stub(bin_dir, "flock", "exit 0")
    _stub(bin_dir, "timeout", 'shift; exec "$@"')
    for tool in ("docker", "curl", "rsync", "chown", "stat"):
        _stub(bin_dir, tool, "exit 0")

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_DIR": str(state),
        "DEPLOY_REPO": str(deploy_repo),
        "COMPOSE_DIR": str(deploy_repo / "infra" / "compose"),
        "MIRROR_DIR": str(mirror),
    }
    return {"env": env}


@pytest.mark.parametrize(("changed_path", "expected_detail"), _CONFIG_MANUAL_SOURCES)
def test_autodeploy_signals_config_manual(
    deploy_box: dict[str, object], changed_path: str, expected_detail: str | None
) -> None:
    env = dict(deploy_box["env"])  # type: ignore[arg-type]
    env["CHANGED_PATH"] = changed_path
    env["DEPLOYED_BODY"] = "[gerrit]\n  canonicalWebUrl = https://old.example\n"
    env["TARGET_BODY"] = "[gerrit]\n  canonicalWebUrl = https://new.example\n"
    result = subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    context = (
        f"changed={changed_path}\nrc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    match = re.search(r"^AUTODEPLOY_ERROR (\{.*\})$", result.stderr, re.MULTILINE)
    assert match, (
        f"a change to the gerrit-side detect-only source {changed_path} must emit an "
        "AUTODEPLOY_ERROR signal — otherwise it reaches /opt/rebar and silently never "
        "applies, since autodeploy never restarts Gerrit.\n"
        f"{context}"
    )
    payload = json.loads(match.group(1))
    assert payload["reason"] == "config_manual", (
        "the signal must use reason=config_manual (the detect-only class), got "
        f"{payload['reason']!r}.\n{context}"
    )
    if expected_detail is not None:
        assert expected_detail in payload["detail"], (
            "the operator-facing detail must name the changed source so the pending manual "
            f"apply is actionable, got {payload['detail']!r}.\n{context}"
        )
    else:
        assert "MANUAL operator apply" in payload["detail"], (
            "the config_manual detail must direct the operator to a manual apply, got "
            f"{payload['detail']!r}.\n{context}"
        )


_BASE = "# header comment\n[gerrit]\n  canonicalWebUrl = https://x.example\n"


def _run_config_change(
    deploy_box: dict[str, object],
    deployed: str,
    target: str,
    *,
    target_missing: bool = False,
    path: str = "infra/compose/gerrit.config",
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, str]]]:
    env = dict(deploy_box["env"])  # type: ignore[arg-type]
    env["CHANGED_PATH"] = path
    env["DEPLOYED_BODY"] = deployed
    env["TARGET_BODY"] = target
    if target_missing:
        env["TARGET_MISSING"] = "1"
    result = subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    errors = [
        json.loads(m)
        for m in re.findall(r"^AUTODEPLOY_ERROR (\{.*\})$", result.stderr, re.MULTILINE)
    ]
    return result, errors


@pytest.mark.parametrize(
    ("label", "target"),
    [
        (
            "comment rewritten",
            "# a shorter header\n[gerrit]\n  canonicalWebUrl = https://x.example\n",
        ),
        ("comment removed", "[gerrit]\n  canonicalWebUrl = https://x.example\n"),
        ("indented comment added", _BASE + "  # note\n"),
        (
            "blank lines and trailing space",
            "# header comment\n\n[gerrit]  \n  canonicalWebUrl = https://x.example   \n\n",
        ),
    ],
)
def test_comment_and_whitespace_only_change_is_not_a_manual_apply(
    deploy_box: dict[str, object], label: str, target: str
) -> None:
    """A change with no effect on the config must not page: the operator has nothing to apply."""
    result, errors = _run_config_change(deploy_box, _BASE, target)
    config_errors = [e for e in errors if e["reason"] == "config_manual"]
    assert not config_errors, (
        f"{label}: a change to comments/whitespace only must not emit config_manual, "
        f"got {config_errors}.\nstderr:\n{result.stderr}"
    )


@pytest.mark.parametrize(
    ("label", "target", "target_missing"),
    [
        ("value changed", _BASE.replace("x.example", "y.example"), False),
        (
            "comment edit plus value change",
            "# new header\n[gerrit]\n  canonicalWebUrl = https://y.example\n",
            False,
        ),
        ("shebang is content", "#!/bin/sh\n" + _BASE, False),
        ("semicolon comment is content", _BASE + "; note\n", False),
        ("path unreadable at target", "", True),
    ],
)
def test_real_or_unprovable_change_still_signals_manual_apply(
    deploy_box: dict[str, object], label: str, target: str, target_missing: bool
) -> None:
    """Anything beyond whole-line comments and whitespace, or unreadable, keeps paging."""
    result, errors = _run_config_change(deploy_box, _BASE, target, target_missing=target_missing)
    assert any(e["reason"] == "config_manual" for e in errors), (
        f"{label}: must still emit config_manual.\nstderr:\n{result.stderr}"
    )


def test_comment_only_change_to_a_rendered_template_still_signals(
    deploy_box: dict[str, object],
) -> None:
    """The g2p template's materializer substitutes its placeholder inside comments too, so a
    comment edit there is material and must keep paging."""
    result, errors = _run_config_change(
        deploy_box,
        _BASE,
        "# a shorter header\n[gerrit]\n  canonicalWebUrl = https://x.example\n",
        path="infra/gerrit/gerrit_to_platform.ini.template",
    )
    assert any(e["reason"] == "config_manual" for e in errors), result.stderr
