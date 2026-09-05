"""Writable container layers, and the live set the reaper must never touch
(story ``910b-2d43-4482-4c64``).

Writable container layers are the last of the four root generators ADR 0112 names. They live
INSIDE ``/var/lib/docker/overlay2`` as each container's ``upperdir``, so no image or
build-cache prune reaches them, and on 2026-09-02 nothing measured them at all.

``infra/scripts/container-cap.sh`` bounds them the only way this host allows, and the tests
below exist mostly to pin the difference between what it does and what it is mistaken for:

**The hard ceiling is not available, and the reason is checkable.** overlay2's per-container
``--storage-opt size=`` is refused unless the filesystem backing ``/var/lib/docker`` is XFS
mounted with ``pquota``, and XFS reads quota options at MOUNT time — so on this ROOT filesystem
it needs ``rootflags=pquota`` and a reboot. ``--check-quota`` reports that as a 1/0 reading
rather than letting a runbook assert a ceiling.

**The reaper cannot touch a RUNNING container's writable layer at all**, and for the debris it
can reach it is a mitigation with a fill-rate assumption, not a ceiling.

**Safety is the sharp edge.** A prune that takes Gerrit down is far worse than the debris it
reclaims, and ``autodeploy.sh`` already carries the scar: bug ``9ea3`` reaped an exited mcp
container that ``$MCP_UPSTREAM_FILE`` still pointed at, turning a transient exit into a
permanent 502. Half this file is the protected sets.

Everything drives the REAL script over PATH stubs: no docker daemon, no systemd, no root, no
XFS, no AWS, no CI provider.
"""

from __future__ import annotations

import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "infra" / "scripts" / "container-cap.sh"
CAP_SCRIPT = REPO_ROOT / "infra" / "scripts" / "docker-storage-cap.sh"

GIB = 1024**3


def _share_bytes() -> int:
    """The writable-layer share as ``docker-storage-cap.sh`` states it — never a literal here.

    Reading it from the same single source of truth the script reads is the point: a test that
    hard-coded the number would keep passing while the reaper and the Docker budget's internal
    split drifted apart, which is the failure this indirection removes.
    """
    out = subprocess.run(
        ["bash", str(CAP_SCRIPT), "--print-env"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("DOCKER_CONTAINER_WRITABLE_BYTES="):
            return int(line.split("=", 1)[1])
    raise AssertionError(f"docker-storage-cap.sh stated no writable-layer share:\n{out}")


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Container:
    """One row of the ``docker inspect --size`` census the reaper reads."""

    def __init__(
        self,
        name: str,
        *,
        size: int,
        status: str = "exited",
        finished: str = "2000-01-01T00:00:00.000000000Z",
        compose: str = "",
        service: str = "",
    ) -> None:
        self.name = name
        self.size = size
        self.status = status
        self.finished = finished
        self.compose = compose or "<no value>"
        self.service = service or "<no value>"

    @property
    def cid(self) -> str:
        return f"{abs(hash(self.name)):064x}"[:64]

    def row(self) -> str:
        return "|".join(
            [
                self.cid,
                f"/{self.name}",
                self.status,
                self.finished,
                str(self.size),
                self.compose,
                self.service,
            ]
        )


def _run(
    tmp_path: Path,
    args: list[str],
    *,
    containers: list[Container] | None = None,
    timer_active: bool = True,
    quota_state: str | None = None,
    have_xfs_quota: bool = True,
    unit_dir: Path | None = None,
    env_extra: dict[str, str] | None = None,
    cap_script: Path | None = CAP_SCRIPT,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Drive ``container-cap.sh``; returns ``(result, docker_call_log)``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "docker-calls.log"
    rows = "\n".join(c.row() for c in (containers or []))
    ids = "\n".join(c.cid for c in (containers or []))

    # One docker stub serving both census calls. `ps -a --format {{.ID}}` yields ids; `inspect`
    # yields the census rows; `rm` records its argv and succeeds. Deliberately NOT keyed on the
    # container id for `rm` — the assertions read the log, so a wrong id shows up as a wrong log
    # line rather than a silent success.
    _stub(
        bin_dir,
        "docker",
        f"""
        printf 'docker %s\\n' "$*" >> {calls}
        case "$1" in
          ps) cat <<'IDS'
{ids}
IDS
          ;;
          inspect) cat <<'ROWS'
{rows}
ROWS
          ;;
          rm) exit 0 ;;
        esac
        exit 0
        """,
    )
    _stub(bin_dir, "timeout", 'shift\nexec "$@"')
    _stub(bin_dir, "systemctl", f"exit {0 if timer_active else 3}")
    if have_xfs_quota:
        _stub(bin_dir, "xfs_quota", f"cat <<'STATE'\n{quota_state or ''}\nSTATE\nexit 0")

    env = subprocess_env(
        {
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
            "CONTAINER_UNIT_DIR": str(unit_dir or (tmp_path / "units")),
            "CONTAINER_INSTALLED_PATH": str(tmp_path / "installed" / "rebar-container-cap.sh"),
            "CONTAINER_QUOTA_FS": "/",
            "CONTAINER_CAP_DOCKER_CAP_SH": str(cap_script or (tmp_path / "absent.sh")),
        }
    )
    env.update(env_extra or {})
    result = subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False
    )
    return result, calls


def _removed(calls: Path) -> list[str]:
    """Container ids the reaper actually asked the daemon to remove."""
    if not calls.exists():
        return []
    return [
        line.split()[2] for line in calls.read_text().splitlines() if line.startswith("docker rm ")
    ]


# --------------------------------------------------------------------------------------
# The share: one budget with an internal split, never a second literal
# --------------------------------------------------------------------------------------


def test_print_env_survives_the_eval_observability_consumes_it_with(tmp_path: Path) -> None:
    """``observability.sh`` reads this with ``eval "$(… --print-env)"``. Two of the values are
    not words — the keep-list has a space and the name pattern has ``^(…|…)``, which an eval of a
    BARE assignment parses as a subshell and dies on, taking the whole §2i section with it.
    """
    result, _ = _run(tmp_path, ["--print-env"])
    assert result.returncode == 0, result.stderr
    evaluated = subprocess.run(
        ["bash", "-c", 'eval "$1"; printf %s "$CONTAINER_KEEP_NAME_RE"', "sh", result.stdout],
        capture_output=True,
        text=True,
        check=False,
    )
    assert evaluated.returncode == 0, evaluated.stderr
    assert evaluated.stderr == "", evaluated.stderr
    assert evaluated.stdout == "^(rebar-mcp|compose-mcp-1)"


def test_the_share_comes_from_the_docker_budget_and_not_from_a_second_literal(
    tmp_path: Path,
) -> None:
    """ADR 0112: writable layers live INSIDE ``/var/lib/docker``, so two caps over the same
    bytes would be either double-counted or mutually violable."""
    result, _ = _run(tmp_path, ["--print-env"])
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if line and "=" in line)
    assert int(values["CONTAINER_WRITABLE_BYTES"].strip("'")) == _share_bytes()


def test_an_unreadable_share_reaps_nothing_rather_than_guessing_a_ceiling(
    tmp_path: Path,
) -> None:
    """A reaper running against a guessed number would delete containers to satisfy a ceiling
    nobody chose — strictly worse than not running. ``autodeploy.sh``'s ``prune_docker_caches``
    takes exactly this position on the BuildKit cap."""
    doomed = Container("scratch-build", size=8 * GIB)
    result, calls = _run(
        tmp_path, ["--reap"], containers=[doomed], cap_script=tmp_path / "absent.sh"
    )
    assert result.returncode == 0, result.stderr
    assert _removed(calls) == []
    assert "reaping NOTHING" in result.stderr


# --------------------------------------------------------------------------------------
# The protected sets — a prune that takes Gerrit down is worse than the debris
# --------------------------------------------------------------------------------------


def test_a_running_container_is_never_a_candidate(tmp_path: Path) -> None:
    """The daemon's own filters never list one, and this asserts the script agrees even when a
    census row says ``running`` — the state a container can enter between the census and the
    removal loop."""
    live = Container("compose-gerrit-1", size=8 * GIB, status="running")
    result, calls = _run(tmp_path, ["--reap"], containers=[live])
    assert result.returncode == 0, result.stderr
    assert _removed(calls) == []


@pytest.mark.parametrize(
    ("label_kwargs", "why"),
    [
        (
            {"compose": "compose"},
            "an EXITED compose service is a CRASHED service whose logs are the evidence",
        ),
        (
            {"service": "mcp"},
            "rebar.service is the identity mcp_run_new stamps because compose never sees it",
        ),
    ],
)
def test_a_labelled_container_is_spared_even_when_exited_and_over_share(
    tmp_path: Path, label_kwargs: dict[str, str], why: str
) -> None:
    victim = Container("compose-review-bot-1", size=8 * GIB, **label_kwargs)
    result, calls = _run(tmp_path, ["--reap"], containers=[victim])
    assert result.returncode == 0, result.stderr
    assert _removed(calls) == [], why


@pytest.mark.parametrize("name", ["rebar-mcp-abc123def456-8092", "compose-mcp-1"])
def test_the_mcp_blue_green_set_is_spared_by_name_even_with_no_labels(
    tmp_path: Path, name: str
) -> None:
    """Bug ``9ea3`` is exactly this container: ``autodeploy.sh`` reaps the mcp set itself under a
    guard reading the nginx ``/mcp/`` upstream include, and refuses to reap an exited container
    that is still the live backend because ``--restart always`` would have restored it. This
    reaper cannot replicate that guard, so it does not get to try.
    """
    victim = Container(name, size=8 * GIB)
    result, calls = _run(tmp_path, ["--reap"], containers=[victim])
    assert result.returncode == 0, result.stderr
    assert _removed(calls) == []


def test_the_protected_name_pattern_matches_autodeploys_own_regex() -> None:
    """The two must agree by construction, not by two edited literals: a container autodeploy
    considers its own but this script does not is exactly the unowned one that gets reaped."""
    autodeploy = (REPO_ROOT / "infra" / "scripts" / "autodeploy.sh").read_text()
    assert 'grep -E "^(${MCP_CONTAINER_PREFIX}|${MCP_COMPOSE_CONTAINER})"' in autodeploy
    rendered = subprocess.run(
        ["bash", str(SCRIPT), "--print-env"], capture_output=True, text=True, check=True
    ).stdout
    assert "CONTAINER_KEEP_NAME_RE='^(rebar-mcp|compose-mcp-1)'" in rendered


def test_a_container_inside_the_grace_window_is_spared(tmp_path: Path) -> None:
    """A container that exited seconds ago is one somebody is about to read ``docker logs``
    from. The snapshot janitor's grace-window reasoning, applied to a different tree."""
    fresh = Container("scratch", size=8 * GIB, finished="2999-01-01T00:00:00.000000000Z")
    result, calls = _run(tmp_path, ["--reap"], containers=[fresh])
    assert result.returncode == 0, result.stderr
    assert _removed(calls) == []
    assert "grace window" in result.stderr


def test_volumes_and_force_are_never_passed_and_prune_is_never_used(tmp_path: Path) -> None:
    """Three separate ways to destroy state this reaper must not have.

    ``-f`` would defeat the daemon's own refusal to remove a running container — the guarantee
    ``mcp_retire_image`` already leans on for images. ``-v``/``--volumes`` would take the named
    and bind volumes carrying Gerrit's source-of-truth state. ``docker system prune`` would take
    volumes and networks belonging to services this script never enumerated.
    """
    debris = Container("scratch", size=8 * GIB)
    result, calls = _run(tmp_path, ["--reap"], containers=[debris])
    assert result.returncode == 0, result.stderr
    log = calls.read_text()
    assert "docker rm " in log, "the reaper did not reap at all, so this guard is vacuous"
    for forbidden in (" -f", " --force", " -v", " --volumes", "system prune", "container prune"):
        assert forbidden not in log, f"the reaper passed {forbidden!r}: {log}"


# --------------------------------------------------------------------------------------
# What it does reap
# --------------------------------------------------------------------------------------


def test_unowned_debris_over_the_share_is_reaped(tmp_path: Path) -> None:
    debris = Container("scratch-build", size=8 * GIB)
    result, calls = _run(tmp_path, ["--reap"], containers=[debris])
    assert result.returncode == 0, result.stderr
    assert _removed(calls) == [debris.cid]


def test_nothing_is_reaped_while_the_total_is_under_the_share(tmp_path: Path) -> None:
    """The share is a CEILING, not a target: debris that fits is not a problem, and evicting it
    costs the logs for nothing.

    The size is deliberately 90% of the share — BELOW the ceiling but ABOVE the 80% low-water
    mark the reaping loop aims at. That is the one region where "is the footprint over the
    ceiling?" and "is it over the low-water mark?" disagree, so a reaper that consulted only the
    loop's target would reap a container off a perfectly healthy box. A token-sized container
    would pass either way and prove nothing.
    """
    debris = Container("scratch-build", size=_share_bytes() * 90 // 100)
    result, calls = _run(tmp_path, ["--reap"], containers=[debris])
    assert result.returncode == 0, result.stderr
    assert _removed(calls) == []


def test_reaping_is_oldest_first_and_stops_at_the_low_water_mark(tmp_path: Path) -> None:
    """Reaping to the ceiling itself would evict one entry on every tick once the set hovers at
    the boundary; the 80% low-water mark is what stops that thrash.

    The sizes are chosen so the two policies DISAGREE. Three halves of the share is 1.5x over;
    removing the oldest brings it to exactly 1.0x, which is already under the CEILING but still
    over the 0.8x low-water mark — so a correct reaper takes a second one and the newest
    survives, while a reaper aiming at the ceiling would stop after one.
    """
    half = _share_bytes() // 2
    newest = Container("newest", size=half, finished="2020-03-01T00:00:00.000000000Z")
    middle = Container("middle", size=half, finished="2020-02-01T00:00:00.000000000Z")
    oldest = Container("oldest", size=half, finished="2020-01-01T00:00:00.000000000Z")
    result, calls = _run(tmp_path, ["--reap"], containers=[newest, middle, oldest])
    assert result.returncode == 0, result.stderr
    assert _removed(calls) == [oldest.cid, middle.cid]


def test_it_says_so_when_it_cannot_get_under_the_share(tmp_path: Path) -> None:
    """S4's honesty bar. A reaper that exits 0 having reclaimed nothing is indistinguishable
    from one holding a ceiling, and the loud half is what manufactures false confidence."""
    live = Container("compose-gerrit-1", size=8 * GIB, status="running")
    result, calls = _run(tmp_path, ["--reap"], containers=[live])
    assert result.returncode == 0, result.stderr
    assert _removed(calls) == []
    assert "WARNING" in result.stderr
    assert "RUNNING" in result.stderr
    assert "NOT a ceiling being enforced" in result.stderr
    assert "rootflags=pquota" in result.stderr


# --------------------------------------------------------------------------------------
# The heartbeat, and the quota reading
# --------------------------------------------------------------------------------------


def _install_units(tmp_path: Path, unit_dir: Path) -> None:
    unit_dir.mkdir(parents=True, exist_ok=True)
    rendered = subprocess.run(
        ["bash", str(SCRIPT), "--print-units"],
        capture_output=True,
        text=True,
        check=True,
        env=subprocess_env({"CONTAINER_UNIT_DIR": str(unit_dir)}),
    ).stdout
    current: Path | None = None
    for line in rendered.splitlines():
        if line.startswith("# ---- "):
            current = unit_dir / line.split()[2]
            current.write_text("")
            continue
        if current is not None:
            with current.open("a") as handle:
                handle.write(line + "\n")


def test_check_active_is_one_when_the_units_match_and_the_timer_runs(tmp_path: Path) -> None:
    unit_dir = tmp_path / "units"
    _install_units(tmp_path, unit_dir)
    result, _ = _run(tmp_path, ["--check-active"], unit_dir=unit_dir, timer_active=True)
    assert result.stdout.strip() == "1", result.stderr


def test_check_active_is_zero_when_the_timer_is_dead(tmp_path: Path) -> None:
    """An installed unit with a dead timer is the state most likely to be mistaken for a working
    ceiling: usage reads nominal while nothing enforces anything. This is the reading
    ``rebar-container-reaper-not-active`` alarms on."""
    unit_dir = tmp_path / "units"
    _install_units(tmp_path, unit_dir)
    result, _ = _run(tmp_path, ["--check-active"], unit_dir=unit_dir, timer_active=False)
    assert result.stdout.strip() == "0", result.stderr


def test_check_active_is_zero_when_the_installed_timer_is_stale(tmp_path: Path) -> None:
    """A stale period is a DIFFERENT ceiling, and the reaper's bound is stated per period."""
    unit_dir = tmp_path / "units"
    _install_units(tmp_path, unit_dir)
    timer = unit_dir / "rebar-container-reaper.timer"
    timer.write_text(timer.read_text().replace("5min", "90min"))
    result, _ = _run(tmp_path, ["--check-active"], unit_dir=unit_dir, timer_active=True)
    assert result.stdout.strip() == "0", result.stderr


def test_check_active_is_zero_when_the_units_are_absent(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, ["--check-active"], unit_dir=tmp_path / "nowhere")
    assert result.stdout.strip() == "0", result.stderr


@pytest.mark.parametrize(
    ("state", "expected", "why"),
    [
        ("User quota on / (/dev/nvme0n1p1)\n  Accounting: ON\n  Enforcement: ON", "1", "enforced"),
        (
            "Project quota on / (/dev/nvme0n1p1)\n  Accounting: ON\n  Enforcement: OFF",
            "0",
            "accounting alone MEASURES and bounds nothing; reporting it as a ceiling would be "
            "the paper bound this epic exists to remove",
        ),
        ("", "0", "an unreadable state fails CLOSED"),
    ],
)
def test_check_quota_only_reports_one_for_real_enforcement(
    tmp_path: Path, state: str, expected: str, why: str
) -> None:
    result, _ = _run(tmp_path, ["--check-quota"], quota_state=state)
    assert result.stdout.strip() == expected, why


def test_check_quota_is_zero_without_xfs_quota(tmp_path: Path) -> None:
    """The default state of this host: no XFS project quota, therefore no per-container overlay2
    ceiling is possible at all, therefore the reaper is the whole story."""
    result, _ = _run(tmp_path, ["--check-quota"], have_xfs_quota=False)
    assert result.stdout.strip() == "0", result.stderr


# --------------------------------------------------------------------------------------
# Rendering, and the side-effect-free contract
# --------------------------------------------------------------------------------------


def test_the_service_start_timeout_nests_below_the_timer_period(tmp_path: Path) -> None:
    """Bug ``1205``: a ``Type=oneshot`` with no ``TimeoutStartSec`` gets an INFINITE start
    timeout, and because ``OnUnitActiveSec`` is measured from the last COMPLETED activation one
    overrun does not delay the next elapse — it DELETES it. A reaper that latches off is a
    ceiling that silently stops existing."""
    result, _ = _run(tmp_path, ["--print-units"])
    assert result.returncode == 0, result.stderr
    timeout = int(
        next(
            line.split("=", 1)[1]
            for line in result.stdout.splitlines()
            if line.startswith("TimeoutStartSec=")
        )
    )
    period_min = int(
        next(
            line.split("=", 1)[1].removesuffix("min")
            for line in result.stdout.splitlines()
            if line.startswith("OnUnitActiveSec=")
        )
    )
    assert timeout < period_min * 60


@pytest.mark.parametrize(
    "mode", ["--print-env", "--print-units", "--check-active", "--check-quota"]
)
def test_the_read_modes_never_touch_the_daemon_or_the_disk(tmp_path: Path, mode: str) -> None:
    """The ``journald-cap.sh`` / ``vartmp-cap.sh`` contract: rendering and both activation checks
    are testable without root, without systemd and without a daemon — and, more to the point,
    observability.sh calls two of them every five minutes and must never reap as a side effect."""
    unit_dir = tmp_path / "units"
    result, calls = _run(
        tmp_path,
        [mode],
        containers=[Container("scratch", size=8 * GIB)],
        unit_dir=unit_dir,
    )
    assert result.returncode == 0, result.stderr
    assert _removed(calls) == []
    assert not unit_dir.exists(), "a read-only mode wrote unit files"


def test_an_unknown_argument_is_refused(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, ["--reap-everything"])
    assert result.returncode != 0
    assert "unknown argument" in result.stderr
