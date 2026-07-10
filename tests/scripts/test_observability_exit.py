"""Exit-code contract for infra/scripts/observability.sh (ticket 8a37).

observability.sh is run by a systemd oneshot timer (install-observability.sh).
A oneshot service is only recorded ``active (exited)`` when its process exits 0;
a nonzero exit marks the unit ``failed`` and trips the deploy/health alarms —
masking real failures behind a probe that is itself always red.

The bug: on a healthy box every metric section is a no-op-or-success, and the
script's terminal statement is ``[ "$oos" -gt 0 ] && logger …``. When the mirror
is in sync ``oos=0``, so ``[ 0 -gt 0 ]`` is false, the ``&&`` short-circuits, and
that false test — the last command in the script, under ``set -uo pipefail`` with
no ``set -e`` and no trailing ``exit 0`` — becomes the script's exit status: 1.

This test drives the whole script under a PATH shim that makes the mirror check
report *in sync* (the healthy path) and asserts the process exits 0.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"
_SHA = "a" * 40


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    p.chmod(0o755)


@pytest.fixture()
def healthy_env(tmp_path: Path) -> dict[str, str]:
    """PATH-shim env where every external probe reports a healthy, in-sync box."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    # curl: gerrit branch REST returns a revision; health probes return 200;
    # IMDS token/region/instance-id return a dummy token.
    _stub(
        bin_dir,
        "curl",
        f"""
        for a in "$@"; do
          case "$a" in
            *projects/rebar/branches/main*)
              printf ")]}}'\\n"; printf '{{"revision": "{_SHA}"}}\\n'; exit 0 ;;
          esac
        done
        case "$*" in *http_code*) printf '200'; exit 0 ;; esac
        printf 'dummy-token'; exit 0
        """,
    )
    # git ls-remote returns the SAME sha as gerrit -> mirror in sync (oos=0).
    _stub(bin_dir, "git", f'printf "{_SHA}\\trefs/heads/main\\n"; exit 0')
    # aws / logger / journalctl are quiet no-op successes.
    _stub(bin_dir, "aws", "exit 0")
    _stub(bin_dir, "logger", "exit 0")
    _stub(bin_dir, "journalctl", "exit 0")

    off = tmp_path / "offsets"
    off.mkdir()
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    # Redirect every persisted offset file away from the real host (/var/lib/rebar).
    env.update(
        {
            "REPL_OFFSET_FILE": str(off / "repl"),
            "VOTER_OFFSET_FILE": str(off / "voter"),
            "MERGE_OFFSET_FILE": str(off / "merge"),
            "DEPLOY_OFFSET_FILE": str(off / "deploy"),
            "G2P_OFFSET_FILE": str(off / "g2p"),
            "REPL_LOG": str(off / "nonexistent-replication-log"),
        }
    )
    return env


def test_observability_exits_zero_on_healthy_run(healthy_env: dict[str, str]) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=healthy_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        "observability.sh must exit 0 on a healthy run so the systemd oneshot is "
        f"recorded active(exited); got {result.returncode}. stderr:\n{result.stderr}"
    )
