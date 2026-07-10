"""autodeploy.sh disk-pressure hardening (incident 2731 follow-up, ticket e2c5).

Drives the ``prune_docker_caches`` / ``record_backoff_failure`` helpers from
``infra/scripts/autodeploy.sh`` in a bash subprocess with a PATH-front ``docker``
(and ``timeout``) stub that logs argv — no docker daemon involved. What must hold:

* every failure exit reclaims: ``record_backoff_failure`` runs one CAPPED
  ``builder prune -f --keep-storage <cap>`` and one ``image prune -f``;
* a prune failure is inert (backoff still recorded, helper returns 0, one
  non-fatal log line) — it can never mask the deploy-failure exit code;
* no uncapped ``docker builder prune`` and no bare ``docker image prune``
  outside the helper exist in the script (the quantified-bound ACs).
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "infra" / "scripts" / "autodeploy.sh"


def _write_stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(f"#!/usr/bin/env bash\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_helpers(tmp_path: Path, *, docker_exit: int, drive: str) -> subprocess.CompletedProcess:
    """Source autodeploy.sh's function definitions (guarded from executing the
    deploy flow by an early no-op environment) and drive one helper."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    # `timeout` stub: portable (absent on stock macOS), drops the duration and
    # execs the wrapped command so the docker stub still records real argv.
    _write_stub(bindir, "timeout", 'shift\nexec "$@"')
    _write_stub(bindir, "docker", f'echo "docker $*" >> "{calls}"\nexit {docker_exit}')
    # Extract the tunables block (everything above the single-flight lock — the
    # script's executable flow starts there) plus the two helper functions under
    # test, which are defined further down (a shell function block ends at the
    # first column-0 closing brace).
    src = SCRIPT.read_text()
    prefix = src.split("# ── single-flight")[0]
    funcs = "\n".join(
        m.group(0)
        for m in re.finditer(
            r"^(?:prune_docker_caches|record_backoff_failure)\(\) \{.*?^\}", src, re.S | re.M
        )
    )
    assert funcs, "helper functions not found in autodeploy.sh"
    harness = f"""
set -uo pipefail
STATE_DIR={tmp_path}/state
{prefix}
{funcs}
TARGET=deadbeef
bo_cnt=3
{drive}
"""
    return subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )


def _calls(tmp_path: Path) -> list[str]:
    p = tmp_path / "calls.log"
    return p.read_text().splitlines() if p.exists() else []


def test_failure_path_prunes_and_records_backoff(tmp_path):
    res = _run_helpers(tmp_path, docker_exit=0, drive="record_backoff_failure")
    assert res.returncode == 0, res.stderr
    calls = _calls(tmp_path)
    assert calls == [
        "docker builder prune -f --keep-storage 5GB",
        "docker image prune -f",
    ]
    backoff = (tmp_path / "state" / "deploy-backoff").read_text().split()
    assert backoff[0] == "deadbeef"
    assert backoff[1] == "4"  # bo_cnt=3 -> fail #4 (prune did not disturb it)


def test_prune_failure_is_inert_and_logged(tmp_path):
    res = _run_helpers(tmp_path, docker_exit=1, drive="prune_docker_caches")
    assert res.returncode == 0, res.stderr  # a failing prune never propagates
    assert "builder prune failed (non-fatal)" in res.stdout
    assert "image prune failed (non-fatal)" in res.stdout


def test_success_path_uses_the_helper(tmp_path):
    res = _run_helpers(tmp_path, docker_exit=0, drive="prune_docker_caches")
    assert res.returncode == 0, res.stderr
    assert _calls(tmp_path) == [
        "docker builder prune -f --keep-storage 5GB",
        "docker image prune -f",
    ]


def test_no_uncapped_or_stray_prunes_in_script():
    src = SCRIPT.read_text()
    # comment-stripped code lines (trailing comments too — the tunable line
    # mentions the flag in its comment and must not count as an invocation).
    code = [ln.split("#")[0] for ln in src.splitlines()]
    builder_prunes = [ln for ln in code if "docker builder prune" in ln]
    assert builder_prunes, "the capped builder prune must exist"
    assert all("--keep-storage" in ln for ln in builder_prunes)
    assert all("timeout" in ln for ln in builder_prunes)  # wedged-daemon bound
    # exactly one image prune — the helper's; the old bare success-path one is gone.
    assert len(re.findall(r"docker image prune", src)) == 1
    # both paths call the helper: the failure seam and the success path.
    assert src.count("prune_docker_caches") >= 3  # def + 2 call sites
