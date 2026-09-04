"""The Docker storage budget and its daemon-native BuildKit GC policy (story 9183).

``infra/scripts/docker-storage-cap.sh`` is the SINGLE source of truth for the
``/var/lib/docker`` budget of ADR 0112 decision 1 and for its internal split, and it renders
the daemon's own ``builder.gc`` policy into ``/etc/docker/daemon.json``. Three properties are
load-bearing and none of them is observable from a passing deploy:

* **The rendered key must match the ENGINE VERSION.** Docker Engine 25.0 / BuildKit 0.13
  introduced ``maxUsedSpace`` and deprecated ``defaultKeepStorage``. A key the running daemon
  does not recognise is SILENTLY IGNORED — the config looks installed and the cap simply does
  not exist, which is indistinguishable from a healthy box until the disk fills.
* **The merge must preserve the daemon's other configuration.** ``daemon.json`` on this host
  is not ours alone; clobbering it is how a "storage cap" turns into an outage.
* **A rejected config must change NOTHING.** A malformed ``daemon.json`` stops ``dockerd``
  starting — a self-inflicted outage on the very host the cap is protecting — so validation
  happens against a temporary file and only a VALID render is moved into place.

These drive the real script in a bash subprocess over PATH-front ``docker`` / ``dockerd`` /
``systemctl`` stubs; no daemon is involved and nothing under ``/etc`` is touched.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "infra" / "scripts" / "docker-storage-cap.sh"


def _stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _bindir(
    tmp_path: Path,
    *,
    server_version: str = "25.0.3",
    dockerd_validate_exit: int = 0,
    systemctl_active: bool = True,
    systemctl_reload_exit: int = 0,
    daemon_pid: int = 4242,
) -> tuple[Path, Path]:
    """A PATH-front stub dir plus the log every stub appends its argv to."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    calls = tmp_path / "calls.log"
    calls.write_text("")  # each stub set starts from a clean argv log
    _stub(
        bindir,
        "docker",
        f"""
        echo "docker $*" >> "{calls}"
        printf '%s\\n' "{server_version}"
        """,
    )
    _stub(
        bindir,
        "dockerd",
        f"""
        echo "dockerd $*" >> "{calls}"
        exit {dockerd_validate_exit}
        """,
    )
    _stub(
        bindir,
        "systemctl",
        f"""
        echo "systemctl $*" >> "{calls}"
        case "$1" in
          is-active) exit {0 if systemctl_active else 3} ;;
          reload)    exit {systemctl_reload_exit} ;;
          show)      printf '%s\\n' "{daemon_pid}"; exit 0 ;;
        esac
        exit 0
        """,
    )
    _stub(bindir, "logger", "exit 0")
    return bindir, calls


def _run(
    tmp_path: Path,
    args: list[str],
    *,
    bindir: Path | None = None,
    daemon_json: Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if bindir is None:
        bindir, _ = _bindir(tmp_path)
    env = subprocess_env()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    if daemon_json is not None:
        env["DOCKER_DAEMON_JSON"] = str(daemon_json)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _proc_dir(tmp_path: Path, *, pid: int = 4242, started_at: float | None = None) -> Path:
    """A stand-in for ``/proc`` whose ``<pid>`` entry carries a chosen start time.

    ``docker-storage-cap.sh`` dates the RUNNING daemon by the mtime of its ``/proc/<pid>``
    directory, which the kernel sets when it creates the process. Passing ``started_at=None``
    creates no entry at all, which is the "start time unreadable" case.
    """
    root = tmp_path / "proc"
    root.mkdir(exist_ok=True)
    if started_at is not None:
        entry = root / str(pid)
        entry.mkdir(exist_ok=True)
        os.utime(entry, (started_at, started_at))
    return root


def _builder_gc(stdout: str) -> dict:
    return json.loads(stdout)["builder"]["gc"]


# --------------------------------------------------------------------------------------
# The budget itself
# --------------------------------------------------------------------------------------


def test_print_env_states_the_whole_budget_and_its_split(tmp_path: Path) -> None:
    """One place answers "how big is the budget, and how is it divided"."""
    result = _run(tmp_path, ["--print-env"], daemon_json=tmp_path / "daemon.json")
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if line and "=" in line)
    budget = int(values["DOCKER_BUDGET_BYTES"])
    buildkit = int(values["DOCKER_BUILDKIT_CACHE_BYTES"])
    image = int(values["DOCKER_IMAGE_SHARE_BYTES"])
    # ADR 0112: ONE budget with an internal split, never two independent caps — the shares
    # must therefore add up to the budget by construction, not by two edited literals.
    assert budget == buildkit + image
    assert budget == 20 * 1024**3
    assert buildkit == 5 * 1024**3


def test_the_split_follows_an_operator_override(tmp_path: Path) -> None:
    """ADR 0112 decision 6: measured DEFAULTS, never frozen constants."""
    result = _run(
        tmp_path,
        ["--print-env"],
        daemon_json=tmp_path / "daemon.json",
        env_extra={
            "DOCKER_BUDGET_BYTES": str(30 * 1024**3),
            "DOCKER_BUILDKIT_CACHE_BYTES": str(8 * 1024**3),
        },
    )
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if line and "=" in line)
    assert int(values["DOCKER_IMAGE_SHARE_BYTES"]) == 22 * 1024**3


def test_a_buildkit_share_larger_than_the_budget_is_refused(tmp_path: Path) -> None:
    """A split that cannot fit inside its own budget is a typo, not a policy."""
    result = _run(
        tmp_path,
        ["--print-env"],
        daemon_json=tmp_path / "daemon.json",
        env_extra={
            "DOCKER_BUDGET_BYTES": str(4 * 1024**3),
            "DOCKER_BUILDKIT_CACHE_BYTES": str(5 * 1024**3),
        },
    )
    assert result.returncode != 0
    assert "budget" in (result.stderr + result.stdout).lower()


# --------------------------------------------------------------------------------------
# Engine-version-dependent rendering
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["25.0.3", "26.1.4", "28.0.0"])
def test_modern_engines_get_max_used_space(tmp_path: Path, version: str) -> None:
    """>= 25.0 honours ``maxUsedSpace``; ``defaultKeepStorage`` is deprecated there."""
    result = _run(
        tmp_path,
        ["--print-json", "--engine-version", version],
        daemon_json=tmp_path / "daemon.json",
    )
    assert result.returncode == 0, result.stderr
    gc = _builder_gc(result.stdout)
    assert gc["enabled"] is True
    assert gc["maxUsedSpace"] == str(5 * 1024**3)
    assert "defaultKeepStorage" not in gc


@pytest.mark.parametrize("version", ["24.0.7", "23.0.1", "20.10.25"])
def test_older_engines_get_default_keep_storage(tmp_path: Path, version: str) -> None:
    """< 25.0 does not know ``maxUsedSpace``, and an unknown key is silently ignored."""
    result = _run(
        tmp_path,
        ["--print-json", "--engine-version", version],
        daemon_json=tmp_path / "daemon.json",
    )
    assert result.returncode == 0, result.stderr
    gc = _builder_gc(result.stdout)
    assert gc["enabled"] is True
    assert gc["defaultKeepStorage"] == str(5 * 1024**3)
    assert "maxUsedSpace" not in gc


def test_the_engine_version_is_probed_when_not_supplied(tmp_path: Path) -> None:
    """No ``--engine-version`` means ask the daemon, not assume."""
    bindir, _ = _bindir(tmp_path, server_version="24.0.7")
    result = _run(tmp_path, ["--print-json"], bindir=bindir, daemon_json=tmp_path / "daemon.json")
    assert result.returncode == 0, result.stderr
    assert "defaultKeepStorage" in _builder_gc(result.stdout)


def test_an_unreadable_engine_version_renders_the_modern_schema_and_says_so(
    tmp_path: Path,
) -> None:
    """Failure disposition: degrade to the modern schema plus a loud log, never to silence."""
    bindir, _ = _bindir(tmp_path)
    _stub(bindir, "docker", "exit 1")
    _stub(bindir, "dockerd", 'case "$*" in *--version*) exit 1 ;; esac\nexit 0')
    result = _run(tmp_path, ["--print-json"], bindir=bindir, daemon_json=tmp_path / "daemon.json")
    assert result.returncode == 0, result.stderr
    assert "maxUsedSpace" in _builder_gc(result.stdout)
    assert "version" in result.stderr.lower()


# --------------------------------------------------------------------------------------
# The merge
# --------------------------------------------------------------------------------------


def test_the_merge_preserves_unrelated_daemon_configuration(tmp_path: Path) -> None:
    """``daemon.json`` is not ours alone; clobbering it is how a cap becomes an outage."""
    daemon_json = tmp_path / "daemon.json"
    daemon_json.write_text(
        json.dumps(
            {
                "log-driver": "journald",
                "live-restore": True,
                "builder": {"entitlements": {"security-insecure": False}},
            }
        )
    )
    result = _run(
        tmp_path,
        ["--print-json", "--engine-version", "25.0.3"],
        daemon_json=daemon_json,
    )
    assert result.returncode == 0, result.stderr
    merged = json.loads(result.stdout)
    assert merged["log-driver"] == "journald"
    assert merged["live-restore"] is True
    # A sibling key INSIDE `builder` survives too: the merge is per-key, not per-object.
    assert merged["builder"]["entitlements"] == {"security-insecure": False}
    assert merged["builder"]["gc"]["maxUsedSpace"] == str(5 * 1024**3)


def test_a_missing_daemon_json_is_created_rather_than_treated_as_an_error(
    tmp_path: Path,
) -> None:
    """A box that never had a ``daemon.json`` is the ordinary first-boot case."""
    result = _run(
        tmp_path,
        ["--print-json", "--engine-version", "25.0.3"],
        daemon_json=tmp_path / "absent" / "daemon.json",
    )
    assert result.returncode == 0, result.stderr
    assert _builder_gc(result.stdout)["enabled"] is True


def test_print_json_never_writes_anything(tmp_path: Path) -> None:
    """The print mode is side-effect-free, following ``compose-up.sh --print-volumes``."""
    daemon_json = tmp_path / "daemon.json"
    daemon_json.write_text('{"log-driver": "journald"}')
    before = daemon_json.read_text()
    _run(tmp_path, ["--print-json", "--engine-version", "25.0.3"], daemon_json=daemon_json)
    assert daemon_json.read_text() == before
    assert not list(tmp_path.glob("daemon.json.bak*"))


# --------------------------------------------------------------------------------------
# Install: backup, validate, and the reload/restart boundary
# --------------------------------------------------------------------------------------


def test_install_writes_the_policy_and_backs_up_what_was_there(tmp_path: Path) -> None:
    daemon_json = tmp_path / "daemon.json"
    daemon_json.write_text('{"log-driver": "journald"}')
    result = _run(tmp_path, ["--install"], daemon_json=daemon_json)
    assert result.returncode == 0, result.stderr
    written = json.loads(daemon_json.read_text())
    assert written["log-driver"] == "journald"
    assert written["builder"]["gc"]["enabled"] is True
    backups = list(tmp_path.glob("daemon.json.bak*"))
    assert backups, "the pre-existing daemon.json was replaced without a backup"
    assert json.loads(backups[0].read_text()) == {"log-driver": "journald"}


def test_a_rejected_config_changes_nothing(tmp_path: Path) -> None:
    """A malformed ``daemon.json`` stops ``dockerd`` starting — never move one into place."""
    daemon_json = tmp_path / "daemon.json"
    original = '{"log-driver": "journald"}'
    daemon_json.write_text(original)
    bindir, calls = _bindir(tmp_path, dockerd_validate_exit=1)
    result = _run(tmp_path, ["--install"], bindir=bindir, daemon_json=daemon_json)
    assert result.returncode != 0
    assert daemon_json.read_text() == original
    # …and the daemon is never poked on a path that changed nothing.
    log = calls.read_text() if calls.exists() else ""
    assert "systemctl reload" not in log
    assert "systemctl restart" not in log


def test_a_second_install_is_idempotent_and_writes_nothing(tmp_path: Path) -> None:
    """Re-running the boot orchestrator must not rewrite a file that is already correct."""
    daemon_json = tmp_path / "daemon.json"
    first = _run(tmp_path, ["--install"], daemon_json=daemon_json)
    assert first.returncode == 0, first.stderr
    settled = daemon_json.read_text()

    bindir, _calls = _bindir(tmp_path)
    second = _run(tmp_path, ["--install"], bindir=bindir, daemon_json=daemon_json)
    assert second.returncode == 0, second.stderr
    assert daemon_json.read_text() == settled
    assert not list(tmp_path.glob("daemon.json.candidate.*"))


# --------------------------------------------------------------------------------------
# INSTALLED is not IN FORCE, and the difference is the whole point
# --------------------------------------------------------------------------------------
# `builder.gc` is read by dockerd at STARTUP and nowhere else. A SIGHUP reload
# (`systemctl reload docker`) applies a fixed key set that does NOT include it: the reload
# exits 0 and the GC policy is unchanged. Patchset 1 of this change reported "the new
# builder.gc policy is in effect" on that exit status — ABSENCE OF EXECUTION REPORTED AS
# SUCCESS, the fourth instance of that defect class in this repo (bugs 9a17, 90c7, 1ef8). A
# cap that reports itself installed while not in force is worse than no cap: the loud half
# manufactures the confidence that stops anyone checking.


def test_a_reload_is_never_used_as_evidence_that_the_policy_took(tmp_path: Path) -> None:
    """The false signal is gone at the source: no reload is attempted at all.

    A reload cannot apply ``builder.gc``, so the only thing it ever produced here was a zero
    exit status that read as activation. Restarting is likewise never done — that takes
    Gerrit, the review-bot and the on-box MCP server down together.
    """
    daemon_json = tmp_path / "daemon.json"
    daemon_json.write_text('{"log-driver": "journald"}')
    bindir, calls = _bindir(tmp_path, systemctl_active=True)
    result = _run(
        tmp_path,
        ["--install"],
        bindir=bindir,
        daemon_json=daemon_json,
        env_extra={"DOCKER_PROC_DIR": str(_proc_dir(tmp_path, started_at=time.time() - 3600))},
    )
    assert result.returncode == 0, result.stderr
    log = calls.read_text()
    assert "systemctl reload" not in log, log
    assert "systemctl restart" not in log, log


def test_a_daemon_older_than_the_policy_is_reported_as_not_in_effect(tmp_path: Path) -> None:
    """The install path ALWAYS lands here: the file was just rewritten under a live daemon."""
    daemon_json = tmp_path / "daemon.json"
    daemon_json.write_text('{"log-driver": "journald"}')
    bindir, _ = _bindir(tmp_path, systemctl_active=True)
    result = _run(
        tmp_path,
        ["--install"],
        bindir=bindir,
        daemon_json=daemon_json,
        env_extra={"DOCKER_PROC_DIR": str(_proc_dir(tmp_path, started_at=time.time() - 3600))},
    )
    # The config IS installed — only its activation is deferred to an operator.
    assert result.returncode == 0, result.stderr
    assert json.loads(daemon_json.read_text())["builder"]["gc"]["enabled"] is True
    assert "is NOT in effect" in result.stderr, result.stderr
    assert "review-bot-ops.md" in result.stderr
    assert "IS in effect" not in result.stderr


def test_a_daemon_started_after_the_policy_is_reported_in_effect(tmp_path: Path) -> None:
    """The one path on which "in effect" is TRUE — and it is observed, not assumed.

    A settled file plus a daemon that started after it was written is the state an operator
    reaches by scheduling the restart the warning above asks for, so re-running ``--install``
    is how they confirm the cap is real.
    """
    daemon_json = tmp_path / "daemon.json"
    assert _run(tmp_path, ["--install"], daemon_json=daemon_json).returncode == 0

    bindir, calls = _bindir(tmp_path, systemctl_active=True)
    result = _run(
        tmp_path,
        ["--install"],
        bindir=bindir,
        daemon_json=daemon_json,
        env_extra={"DOCKER_PROC_DIR": str(_proc_dir(tmp_path, started_at=time.time() + 60))},
    )
    assert result.returncode == 0, result.stderr
    assert "IS in effect" in result.stderr, result.stderr
    assert "is NOT in effect" not in result.stderr
    assert "systemctl reload" not in calls.read_text()


def test_an_unreadable_daemon_start_time_fails_closed(tmp_path: Path) -> None:
    """Undeterminable is reported as NOT in effect: over-claiming here costs a full disk."""
    daemon_json = tmp_path / "daemon.json"
    assert _run(tmp_path, ["--install"], daemon_json=daemon_json).returncode == 0

    bindir, _ = _bindir(tmp_path, systemctl_active=True)
    result = _run(
        tmp_path,
        ["--install"],
        bindir=bindir,
        daemon_json=daemon_json,
        # No /proc/<pid> entry at all — the daemon is live but undatable.
        env_extra={"DOCKER_PROC_DIR": str(_proc_dir(tmp_path, started_at=None))},
    )
    assert result.returncode == 0, result.stderr
    assert "is NOT in effect" in result.stderr, result.stderr


def test_an_inactive_daemon_is_told_it_will_read_the_policy_at_start(tmp_path: Path) -> None:
    """First boot: ``compose-up.sh`` starts Docker next, so it picks the cap up for free."""
    daemon_json = tmp_path / "daemon.json"
    bindir, calls = _bindir(tmp_path, systemctl_active=False)
    result = _run(tmp_path, ["--install"], bindir=bindir, daemon_json=daemon_json)
    assert result.returncode == 0, result.stderr
    assert "when it next starts" in result.stderr, result.stderr
    assert "systemctl reload" not in (calls.read_text() if calls.exists() else "")


# --------------------------------------------------------------------------------------
# The undo set is bounded — this is a DISK-CEILING script
# --------------------------------------------------------------------------------------


def test_repeated_installs_do_not_accumulate_unbounded_backups(tmp_path: Path) -> None:
    """A cap that grows its own on-disk set on every run is arguing against itself.

    ``--install`` takes a timestamped ``daemon.json.bak.<epoch>`` before it replaces anything,
    which is the operator's undo for a policy that turns out wrong on this box. Patchset 1
    never pruned them, so a boot orchestrator that re-runs leaves one copy behind per run,
    forever, on the volume the story exists to bound.
    """
    daemon_json = tmp_path / "daemon.json"
    daemon_json.write_text('{"log-driver": "journald"}')
    stale = [tmp_path / f"daemon.json.bak.{1700000000 + n}" for n in range(1, 10)]
    for path in stale:
        path.write_text('{"log-driver": "journald"}')

    result = _run(tmp_path, ["--install"], daemon_json=daemon_json)
    assert result.returncode == 0, result.stderr

    kept = sorted(p.name for p in tmp_path.glob("daemon.json.bak.*"))
    assert len(kept) == 5, kept
    # The five NEWEST survive: this run's own backup plus the four most recent stale ones.
    assert kept[0] == "daemon.json.bak.1700000006", kept
    for path in stale[:5]:
        assert not path.exists(), f"{path.name} should have been pruned"


# --------------------------------------------------------------------------------------
# compose-up.sh calls it, and WHERE it calls it is the contract
# --------------------------------------------------------------------------------------
# These RUN the boot orchestrator over stubs rather than reading its source. An earlier
# revision asserted a verbatim line of the script under test and string-sliced around it,
# which passes for the wrong reason on a rename and fails for the wrong reason on a
# behaviour-preserving edit. What is actually load-bearing is the ORDER two steps execute in
# and whether the second one still executes when the first fails — both observable.

COMPOSE_UP = REPO_ROOT / "infra" / "scripts" / "compose-up.sh"


def _boot_sandbox(tmp_path: Path, *, cap_install_exit: int) -> tuple[Path, Path, Path]:
    """A copy of ``compose-up.sh`` beside a stub cap script, plus PATH stubs and a call log.

    ``compose-up.sh`` resolves its siblings through ``BASH_SOURCE``, so the copy is what makes
    the cap script substitutable at all. Everything the boot path shells out to is stubbed and
    logged; the run is expected to die somewhere further down, which is fine — the assertions
    are about what had already happened by then.
    """
    scripts = tmp_path / "repo" / "infra" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "compose-up.sh").write_text(COMPOSE_UP.read_text())
    calls = tmp_path / "boot-calls.log"
    calls.write_text("")
    _stub(
        scripts,
        "docker-storage-cap.sh",
        f"""
        echo "docker-storage-cap --install" >> "{calls}"
        exit {cap_install_exit}
        """,
    )
    bindir = tmp_path / "bootbin"
    bindir.mkdir()
    for name in ("systemctl", "dnf", "curl", "docker", "aws", "logger"):
        _stub(bindir, name, f'echo "{name} $*" >> "{calls}"\nexit 0\n')
    return scripts / "compose-up.sh", bindir, calls


def _boot(script: Path, bindir: Path) -> subprocess.CompletedProcess[str]:
    env = subprocess_env()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def test_compose_up_installs_the_cap_before_it_starts_docker(tmp_path: Path) -> None:
    """``systemctl enable --now docker`` is what READS /etc/docker/daemon.json.

    Installing the policy after it means a first boot brings the daemon up unbounded and stays
    that way until something restarts it — which on this host is an operator-scheduled outage
    window, not a routine event. Ordering is invisible in a passing deploy, so it is pinned by
    executing both steps and reading the order they actually ran in.
    """
    script, bindir, calls = _boot_sandbox(tmp_path, cap_install_exit=0)
    _boot(script, bindir)
    log = calls.read_text().splitlines()
    install = [i for i, line in enumerate(log) if line.startswith("docker-storage-cap")]
    start = [i for i, line in enumerate(log) if line.startswith("systemctl enable --now docker")]
    assert install, f"compose-up.sh never ran the Docker storage cap installer:\n{log}"
    assert start, f"compose-up.sh never started docker — re-derive this ordering guard:\n{log}"
    assert max(install) < min(start), (
        f"compose-up.sh installs the builder.gc policy AFTER starting Docker, so a first boot "
        f"comes up with no BuildKit cap at all:\n{log}"
    )


def test_a_failed_cap_install_does_not_stop_the_stack_booting(tmp_path: Path) -> None:
    """An uncapped BuildKit cache is a capacity problem, not a reason to refuse to boot.

    It is also not invisible: ``rebar-docker-buildkit-cache-high`` alarms on that generator
    directly, which is what makes the non-fatal branch safe rather than merely convenient. The
    observable claim is that the NEXT boot step still ran, under a script that is
    ``set -e`` throughout.
    """
    script, bindir, calls = _boot_sandbox(tmp_path, cap_install_exit=1)
    result = _boot(script, bindir)
    log = calls.read_text()
    assert "docker-storage-cap --install" in log
    assert "systemctl enable --now docker" in log, (
        f"a failed cap install aborted the boot instead of warning:\n{log}"
    )
    assert "WARN" in result.stderr, result.stderr
    assert "UNBOUNDED" in result.stderr, result.stderr
