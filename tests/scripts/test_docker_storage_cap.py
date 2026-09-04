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
import stat
import subprocess
import textwrap
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


def test_a_second_install_is_idempotent_and_does_not_reload(tmp_path: Path) -> None:
    """Re-running the boot orchestrator must not bounce Docker for a no-op."""
    daemon_json = tmp_path / "daemon.json"
    first = _run(tmp_path, ["--install"], daemon_json=daemon_json)
    assert first.returncode == 0, first.stderr
    settled = daemon_json.read_text()

    bindir, calls = _bindir(tmp_path)
    second = _run(tmp_path, ["--install"], bindir=bindir, daemon_json=daemon_json)
    assert second.returncode == 0, second.stderr
    assert daemon_json.read_text() == settled
    log = calls.read_text() if calls.exists() else ""
    assert "systemctl reload" not in log


def test_a_changed_policy_on_a_live_daemon_reloads_and_never_restarts(tmp_path: Path) -> None:
    """A restart takes Gerrit, the review-bot and MCP down: that stays an operator call."""
    daemon_json = tmp_path / "daemon.json"
    daemon_json.write_text('{"log-driver": "journald"}')
    bindir, calls = _bindir(tmp_path, systemctl_active=True)
    result = _run(tmp_path, ["--install"], bindir=bindir, daemon_json=daemon_json)
    assert result.returncode == 0, result.stderr
    log = calls.read_text()
    assert "systemctl reload docker" in log
    assert "restart" not in log


def test_a_failed_reload_warns_toward_the_runbook_and_still_never_restarts(
    tmp_path: Path,
) -> None:
    """``builder.gc`` may not be live-reloadable; the answer is a WARN, not a bounce."""
    daemon_json = tmp_path / "daemon.json"
    daemon_json.write_text('{"log-driver": "journald"}')
    bindir, calls = _bindir(tmp_path, systemctl_active=True, systemctl_reload_exit=1)
    result = _run(tmp_path, ["--install"], bindir=bindir, daemon_json=daemon_json)
    # The config IS installed — only its activation is deferred to an operator.
    assert result.returncode == 0, result.stderr
    assert json.loads(daemon_json.read_text())["builder"]["gc"]["enabled"] is True
    assert "restart" not in calls.read_text()
    assert "review-bot-ops.md" in result.stderr


def test_an_inactive_daemon_is_not_reloaded(tmp_path: Path) -> None:
    """First boot: ``compose-up.sh`` starts Docker next, so it picks the cap up for free."""
    daemon_json = tmp_path / "daemon.json"
    bindir, calls = _bindir(tmp_path, systemctl_active=False)
    result = _run(tmp_path, ["--install"], bindir=bindir, daemon_json=daemon_json)
    assert result.returncode == 0, result.stderr
    log = calls.read_text() if calls.exists() else ""
    assert "systemctl reload" not in log


# --------------------------------------------------------------------------------------
# compose-up.sh calls it, and WHERE it calls it is the contract
# --------------------------------------------------------------------------------------

COMPOSE_UP = REPO_ROOT / "infra" / "scripts" / "compose-up.sh"


def _compose_up_code() -> list[str]:
    """compose-up.sh with comments stripped, so prose about a command is not a call site."""
    return [line.split("#")[0] for line in COMPOSE_UP.read_text().splitlines()]


def test_compose_up_installs_the_cap_before_it_starts_docker() -> None:
    """Ordering is the contract, not a detail.

    ``systemctl enable --now docker`` is what reads ``/etc/docker/daemon.json``. Installing
    the policy AFTER it means a first boot brings the daemon up unbounded and stays that way
    until something restarts it — which on this host is an operator-scheduled outage window,
    not a routine event. Ordering is invisible in a passing deploy, so it is pinned here.
    """
    code = _compose_up_code()
    install = [i for i, line in enumerate(code) if "docker-storage-cap.sh" in line]
    start = [i for i, line in enumerate(code) if "systemctl enable --now docker" in line]
    assert install, "compose-up.sh never installs the Docker storage cap"
    assert start, "compose-up.sh no longer starts docker — re-derive this ordering guard"
    assert max(install) < min(start), (
        "compose-up.sh installs the builder.gc policy AFTER starting Docker, so a first boot "
        "comes up with no BuildKit cap at all"
    )


def test_a_failed_cap_install_does_not_stop_the_stack_booting() -> None:
    """An uncapped BuildKit cache is a capacity problem, not a reason to refuse to boot.

    It is also not invisible: ``rebar-docker-buildkit-cache-high`` alarms on that generator
    directly, which is what makes the non-fatal branch safe rather than merely convenient.
    This follows the ``materialize-*`` steps further down the same script.
    """
    source = COMPOSE_UP.read_text()
    guard = 'if ! bash "${SCRIPT_DIR}/docker-storage-cap.sh" --install; then'
    assert guard in source, (
        "the cap install must be guarded so a failure warns rather than aborting the boot"
    )
    warn = source.split(guard, 1)[1].split("fi", 1)[0]
    assert "WARN" in warn, warn
    assert "exit" not in warn, f"a failed cap install must not abort compose-up:\n{warn}"
