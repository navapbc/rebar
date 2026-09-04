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
CAP_SCRIPT = REPO_ROOT / "infra" / "scripts" / "docker-storage-cap.sh"


def _buildkit_cap() -> str:
    """The BuildKit share as ``docker-storage-cap.sh`` states it — never a literal here.

    Reading the cap from the same single source of truth the script reads is the point: a
    test that hard-coded the number would keep passing while the prune and the daemon's own
    ``builder.gc`` policy drifted apart, which is the failure this indirection removes.
    """
    out = subprocess.run(
        ["bash", str(CAP_SCRIPT), "--print-env"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("DOCKER_BUILDKIT_CACHE_BYTES="):
            return line.split("=", 1)[1]
    raise AssertionError(f"docker-storage-cap.sh --print-env stated no BuildKit share:\n{out}")


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
    used_pct: int = 50,
    extra_funcs: str = "",
    cap_script: Path | None = CAP_SCRIPT,
    env_extra: dict[str, str] | None = None,
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
            f"""
            case "$*" in
              *pcent*) echo "Use%"; printf ' {used_pct}%%\n'; exit 0 ;;
            esac
            echo "Avail"
            cat "{avail}"
            printf '%s' "{free_kb[1]}" > "{avail}"
            exit 0
            """,
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
            (
                r"^(?:prune_docker_caches|record_backoff_failure|root_disk_free_kb|"
                rf"root_disk_pct{extra_funcs})\(\) \{{.*?^\}}"
            ),
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
        # The env is deliberately minimal so the stub PATH is the only one the harness
        # script can see. REBAR_ROOT is pinned explicitly because that minimal mapping
        # drops the unit tier's inherited isolation root, and a child without it falls
        # back to the git toplevel of its cwd — the real checkout.
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "REBAR_ROOT": str(tmp_path),
            # autodeploy.sh reads its BuildKit cap from docker-storage-cap.sh rather than
            # spelling a number. `bash -c` leaves BASH_SOURCE unset, so the script's own
            # sibling-path derivation cannot fire here; point it at the real script (or, for
            # the broken-install case, at nothing) explicitly.
            **({"DOCKER_CAP_SH": str(cap_script)} if cap_script is not None else {}),
            **(env_extra or {}),
        },
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
        f"docker builder prune -f --keep-storage {_buildkit_cap()}",
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
        f"docker builder prune -f --keep-storage {_buildkit_cap()}",
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
        f"docker builder prune -f --keep-storage {_buildkit_cap()}",
        "docker image prune -f",
    ], "the prune still runs when the free-space probe is unreadable"
    assert "before=0kB after=0kB freed=0kB" in res.stdout, res.stdout


def test_hard_pressure_prune_drops_build_cache_keep_and_measures_effect(tmp_path):
    """At the hard pressure tier, the automated prune must be able to reclaim below the
    steady-state warm-cache floor and must report measured bytes freed."""
    res = _run_helpers(
        tmp_path,
        docker_exit=0,
        drive="DISK_PRESSURE_HARD_PCT=90 prune_docker_caches",
        free_kb=(1000, 6200),
        used_pct=93,
    )

    assert res.returncode == 0, res.stderr
    assert _calls(tmp_path) == [
        "docker builder prune -f",
        "docker image prune -f",
    ]
    assert "before=1000kB after=6200kB freed=5200kB" in res.stdout


def test_normal_pressure_prune_keeps_warm_build_cache(tmp_path):
    """Below the hard tier, the existing warm-cache cap remains the contract."""
    res = _run_helpers(
        tmp_path,
        docker_exit=0,
        drive="DISK_PRESSURE_HARD_PCT=90 prune_docker_caches",
        used_pct=85,
    )

    assert res.returncode == 0, res.stderr
    assert _calls(tmp_path) == [
        f"docker builder prune -f --keep-storage {_buildkit_cap()}",
        "docker image prune -f",
    ]


def test_no_uncapped_or_stray_prunes_in_script():
    src = SCRIPT.read_text()
    # comment-stripped code lines (trailing comments too — the tunable line
    # mentions the flag in its comment and must not count as an invocation).
    code = [ln.split("#")[0] for ln in src.splitlines()]
    builder_prunes = [ln.strip() for ln in code if "docker builder prune" in ln]
    assert builder_prunes, "the builder prune must exist"
    assert all("timeout" in ln for ln in builder_prunes)  # wedged-daemon bound
    assert any("--keep-storage" in ln for ln in builder_prunes), "normal prune keeps warm cache"
    uncapped = [ln for ln in builder_prunes if "--keep-storage" not in ln]
    assert uncapped == ["if ! timeout 120 docker builder prune -f >/dev/null 2>&1; then"]
    # exactly one image prune — the helper's; the old bare success-path one is gone. Counted on
    # the COMMENT-STRIPPED lines, like the builder-prune check above: prose explaining why a
    # blanket `docker image prune` cannot reclaim the tagged per-release images (bug 9bc0) names
    # the command without invoking it, and must not read as a second call site.
    assert len([ln for ln in code if "docker image prune" in ln]) == 1
    # both paths call the helper: the failure seam and the success path.
    assert src.count("prune_docker_caches") >= 3  # def + 2 call sites


# --------------------------------------------------------------------------------------
# One budget, read from one place (ADR 0112 decision 1, story 9183)
# --------------------------------------------------------------------------------------


def test_the_keep_storage_cap_comes_from_the_shared_budget(tmp_path):
    """The prune's ceiling and the daemon's own builder.gc ceiling must be ONE number.

    They bound the same bytes from two directions — an on-demand prune and the daemon's own
    garbage collector — so two independently edited literals would let the box enforce two
    different BuildKit caps at once and neither would be the documented one. Moving the budget
    (which is what ``docker-storage-cap.sh`` renders into ``daemon.json``) must move the
    prune's flag with it; if it does not, the value was baked into ``autodeploy.sh``.
    """
    override = str(3 * 1024**3)
    res = _run_helpers(
        tmp_path,
        docker_exit=0,
        drive="prune_docker_caches",
        env_extra={"DOCKER_BUILDKIT_CACHE_BYTES": override},
    )
    assert res.returncode == 0, res.stderr
    assert _calls(tmp_path) == [
        f"docker builder prune -f --keep-storage {override}",
        "docker image prune -f",
    ]


def test_the_buildkit_cap_is_not_re_spelled_in_autodeploy(tmp_path):
    """A second copy of the number is the drift this indirection exists to remove."""
    cap = _buildkit_cap()
    src = SCRIPT.read_text()
    assert cap not in src, (
        f"autodeploy.sh spells the BuildKit cap ({cap}) itself; it must come from "
        "docker-storage-cap.sh --print-env so the prune and the daemon's builder.gc policy "
        "cannot disagree"
    )
    assert "docker-storage-cap.sh" in src


def test_an_unreadable_budget_skips_the_capped_prune_rather_than_guessing(tmp_path):
    """A broken install must not invent a ceiling — losing a warm cache is the cheap failure.

    Falling back to a hard-coded number here would silently re-create the second copy of the
    cap; guessing a *smaller* one would throw away a warm build cache on every tick. Skipping
    is the only option that neither lies nor destroys, and the uncapped state is not invisible:
    ``rebar-docker-buildkit-cache-high`` alarms on the BuildKit generator directly.
    """
    res = _run_helpers(
        tmp_path,
        docker_exit=0,
        drive="prune_docker_caches",
        cap_script=tmp_path / "does-not-exist.sh",
    )
    assert res.returncode == 0, res.stderr
    assert _calls(tmp_path) == ["docker image prune -f"], (
        "with no readable budget the capped builder prune is skipped, not guessed"
    )
    assert "BuildKit cap unavailable" in res.stdout


# --------------------------------------------------------------------------------------
# The image/layer share is held by RETENTION, and retention must not eat the rollback
# --------------------------------------------------------------------------------------


def _orphan_sweep(tmp_path, *, tags: list[str], live: str, prev_sha: str):
    """Drive ``mcp_reconcile_orphans`` with its collaborators stubbed at the shell level."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "mcp-previous-sha").write_text(prev_sha)
    tag_list = "\n".join(tags)
    drive = f"""
mcp_live_port() {{ echo 8092; }}
mcp_image_on_port() {{ echo '{live}'; }}
mcp_image_tags() {{ printf '%s\\n' '{tag_list}'; }}
mcp_reconcile_orphans
"""
    return _run_helpers(tmp_path, docker_exit=0, drive=drive, extra_funcs="|mcp_reconcile_orphans")


def test_the_retention_sweep_spares_the_serving_release_and_the_rollback(tmp_path):
    """Docker has no image-store ceiling, so the image share is held by retention — and a
    retention pass that removed the live or previous release would turn a disk cap into a
    deploy outage. Both are preserved by name; everything else is surplus."""
    live_sha, prev_sha, stale_sha = "a" * 40, "b" * 40, "c" * 40
    res = _orphan_sweep(
        tmp_path,
        tags=[f"compose-mcp:{sha}" for sha in (live_sha, prev_sha, stale_sha)],
        live=f"compose-mcp:{live_sha}",
        prev_sha=prev_sha,
    )
    assert res.returncode == 0, res.stderr
    removals = [ln for ln in _calls(tmp_path) if ln.startswith("docker image rm")]
    assert removals == [f"docker image rm compose-mcp:{stale_sha}"]


def test_the_retention_sweep_never_forces_a_removal(tmp_path):
    """``docker image rm`` WITHOUT ``-f``: the daemon itself refuses while any container
    references the image, which is the guard that holds even if every other one is wrong."""
    live_sha, prev_sha = "a" * 40, "b" * 40
    res = _orphan_sweep(
        tmp_path,
        tags=[f"compose-mcp:{sha}" for sha in (live_sha, prev_sha, "d" * 40)],
        live=f"compose-mcp:{live_sha}",
        prev_sha=prev_sha,
    )
    assert res.returncode == 0, res.stderr
    for line in _calls(tmp_path):
        assert not line.startswith("docker image rm -f"), line
    code = [ln.split("#")[0] for ln in SCRIPT.read_text().splitlines()]
    assert not [ln for ln in code if "image rm" in ln and " -f" in ln]


def test_the_retention_sweep_ignores_non_release_tags(tmp_path):
    """``:prev``, ``:latest``, the bare build tag and ``<none>`` rows are never candidates."""
    live_sha, prev_sha = "a" * 40, "b" * 40
    res = _orphan_sweep(
        tmp_path,
        tags=[
            f"compose-mcp:{live_sha}",
            f"compose-mcp:{prev_sha}",
            "compose-mcp:prev",
            "compose-mcp:latest",
            "compose-mcp:<none>",
        ],
        live=f"compose-mcp:{live_sha}",
        prev_sha=prev_sha,
    )
    assert res.returncode == 0, res.stderr
    assert [ln for ln in _calls(tmp_path) if ln.startswith("docker image rm")] == []
