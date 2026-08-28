"""autodeploy.sh disk-pressure hardening (incident 2731 follow-up, ticket e2c5).

Drives the ``prune_docker_caches`` / ``record_backoff_failure`` helpers from
``infra/scripts/autodeploy.sh`` in a bash subprocess with a PATH-front ``docker``
(and ``timeout``) stub that logs argv — no docker daemon involved. What must hold:

* every failure exit reclaims: ``record_backoff_failure`` runs one CAPPED
  ``builder prune -f --keep-storage <cap>`` and one ``image prune -f``;
* a prune failure is inert (backoff still recorded, helper returns 0, one
  non-fatal log line) — it can never mask the deploy-failure exit code;
* every invocation MEASURES itself: root-disk free space before and after plus
  the delta (bug 9bc0 — the reclaim logged "complete" for ~11h while freeing
  nothing, and nothing in the log could tell the two apart);
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


def _run_helpers(
    tmp_path: Path,
    *,
    docker_exit: int,
    drive: str,
    free_kb: tuple[int, int] | None = (1000, 1000),
) -> subprocess.CompletedProcess:
    """Source autodeploy.sh's function definitions (guarded from executing the
    deploy flow by an early no-op environment) and drive one helper."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    # `timeout` stub: portable (absent on stock macOS), drops the duration and
    # execs the wrapped command so the docker stub still records real argv.
    _write_stub(bindir, "timeout", 'shift\nexec "$@"')
    _write_stub(bindir, "docker", f'echo "docker $*" >> "{calls}"\nexit {docker_exit}')
    # `df --output=avail /` stub: a header line + a file-backed number, matching the
    # `df … | tail -1 | tr -dc '0-9'` shape root_disk_free_kb parses. It is SEQUENCED — the
    # first read yields free_kb[0] and every later read yields free_kb[1] — so the before/after
    # pair straddles the prune and models space it actually reclaimed.
    # free_kb=None models a `df` that yields nothing parseable (absent on stock macOS, a
    # wedged mount, an unsupported --output): the helper must degrade to 0, not blow up.
    if free_kb is None:
        _write_stub(bindir, "df", "exit 1")
    else:
        avail = tmp_path / "avail"
        avail.write_text(str(free_kb[0]))
        _write_stub(
            bindir,
            "df",
            f'echo "Avail"\ncat "{avail}"\nprintf \'%s\' "{free_kb[1]}" > "{avail}"\nexit 0',
        )
    # Extract the tunables block (everything above the single-flight lock — the
    # script's executable flow starts there) plus the helper functions under test
    # and their collaborators, which are defined further down (a shell function
    # block ends at the first column-0 closing brace).
    src = SCRIPT.read_text()
    prefix = src.split("# ── single-flight")[0]
    funcs = "\n".join(
        m.group(0)
        for m in re.finditer(
            r"^(?:prune_docker_caches|record_backoff_failure|root_disk_free_kb)\(\) \{.*?^\}",
            src,
            re.S | re.M,
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
        check=False,
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


def test_prune_logs_free_space_before_after_and_the_delta(tmp_path):
    """Every invocation MEASURES itself. In bug 9bc0-1200-1451-44bb the reclaim ran on schedule,
    logged "disk pressure reclaim complete" every time, and freed nothing — and the log could
    not distinguish that from a reclaim that worked. Before, after, and the delta must all
    appear, so effect is OBSERVED rather than inferred."""
    res = _run_helpers(tmp_path, docker_exit=0, drive="prune_docker_caches", free_kb=(1000, 4200))
    assert res.returncode == 0, res.stderr
    assert "before=1000kB" in res.stdout, f"the BEFORE reading must be logged\n{res.stdout}"
    assert "after=4200kB" in res.stdout, f"the AFTER reading must be logged\n{res.stdout}"
    assert "freed=3200kB" in res.stdout, (
        f"the DELTA is the whole point — a reader must not have to subtract\n{res.stdout}"
    )


def test_prune_still_reports_a_delta_when_it_reclaimed_nothing(tmp_path):
    """The incident case, and the reason the delta is load-bearing: a prune that frees NOTHING
    must say so numerically rather than only logging that it completed."""
    res = _run_helpers(tmp_path, docker_exit=0, drive="prune_docker_caches", free_kb=(900, 900))
    assert res.returncode == 0, res.stderr
    assert "before=900kB after=900kB freed=0kB" in res.stdout, (
        f"an ineffective prune must report freed=0kB\n{res.stdout}"
    )


def test_an_unreadable_df_degrades_to_zero_rather_than_breaking_the_prune(tmp_path):
    """`root_disk_free_kb` echoes 0 when `df` yields nothing parseable. The prune must still run
    and still exit 0 — a broken measurement probe can never become a reclaim failure."""
    res = _run_helpers(tmp_path, docker_exit=0, drive="prune_docker_caches", free_kb=None)
    assert res.returncode == 0, res.stderr
    assert _calls(tmp_path) == [
        "docker builder prune -f --keep-storage 5GB",
        "docker image prune -f",
    ], "the prune still runs when the free-space probe is unreadable"
    assert "before=0kB after=0kB freed=0kB" in res.stdout, res.stdout


def test_no_uncapped_or_stray_prunes_in_script():
    src = SCRIPT.read_text()
    # comment-stripped code lines (trailing comments too — the tunable line
    # mentions the flag in its comment and must not count as an invocation).
    code = [ln.split("#")[0] for ln in src.splitlines()]
    builder_prunes = [ln for ln in code if "docker builder prune" in ln]
    assert builder_prunes, "the capped builder prune must exist"
    assert all("--keep-storage" in ln for ln in builder_prunes)
    assert all("timeout" in ln for ln in builder_prunes)  # wedged-daemon bound
    # exactly one image prune — the helper's; the old bare success-path one is gone. Counted on
    # the COMMENT-STRIPPED lines, like the builder-prune check above: prose explaining why a
    # blanket `docker image prune` cannot reclaim the tagged per-release images (bug 9bc0) names
    # the command without invoking it, and must not read as a second call site.
    assert len([ln for ln in code if "docker image prune" in ln]) == 1
    # both paths call the helper: the failure seam and the success path.
    assert src.count("prune_docker_caches") >= 3  # def + 2 call sites
