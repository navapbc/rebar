"""autodeploy grows an `mcp` blue-green target (panicky-sylphish-foxterrier / ADR 0079).

The on-box `rebar-mcp` server is a NEVER-IDLE shared endpoint, so the review-bot's
stop-and-health-drain deploy cannot be reused: a gauge-gated drain bound can never reach zero
(review-bot bug 7b4a). This target instead models the LOCAL `origin/main` updater — immutable
release + ATOMIC pointer swap + retire-when-idle:

  build+tag -> memory pre-check -> `docker run -d` a NEW container on a free blue/green port ->
  health-check its /health -> ATOMICALLY flip the nginx /mcp/ upstream include + reload
  (the deploy is DONE here, never waiting on an in-flight op) -> RETIRE the old container off
  the critical path with a GRACEFUL `docker stop` (SIGTERM triggers the container's OWN bounded
  self-drain), NEVER `docker rm -f` a serving container.

These are real-subprocess integration tests: the box's binaries are stubbed onto PATH and the
REAL `infra/scripts/autodeploy.sh` runs, so the assertions bind the shipped bash. Every assertion
is OBSERVABLE only — the docker/nginx call log, exit codes, the materialized upstream include
bytes, and the countable journal markers — never internal structure.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

AUTODEPLOY = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "autodeploy.sh"
COMPOSE_FILE = Path(__file__).resolve().parents[2] / "infra" / "compose" / "docker-compose.yml"
_DEPLOYED = "d" * 40
_TARGET = "e" * 40


# --------------------------------------------------------------------------- #
# stub bodies (placeholder-substituted so bash braces need no python escaping) #
# --------------------------------------------------------------------------- #

_DOCKER_STUB = r"""
LOG="__CMDLOG__"
DS="__DSTATE__"
CT="$DS/containers"
touch "$CT"
port_of(){ awk -F'|' -v n="$1" '$1==n{print $2}' "$CT" | head -1; }
state_of(){ awk -F'|' -v n="$1" '$1==n{print $3}' "$CT" | head -1; }
set_state(){
  awk -F'|' -v n="$1" -v s="$2" 'BEGIN{OFS="|"}{if($1==n)$3=s;print}' "$CT" > "$CT.t"
  mv "$CT.t" "$CT"
}
add_ct(){ echo "$1|$2|running" >> "$CT"; }
del_ct(){ grep -v "^$1|" "$CT" > "$CT.t" 2>/dev/null; mv "$CT.t" "$CT" 2>/dev/null || true; }
names_running(){ awk -F'|' '$3=="running"{print $1}' "$CT"; }
names_all(){ awk -F'|' '{print $1}' "$CT"; }

case "$1 $2" in
  "compose build") echo "compose-build-$3" >> "$LOG"; exit 0 ;;
  "compose up")
    last=""; for a in "$@"; do last="$a"; done
    echo "compose-up-$last" >> "$LOG"; exit 0 ;;
  "compose logs")   echo "log-tail"; exit 0 ;;
  "image inspect")  exit 0 ;;
esac
case "$1" in
  tag) echo "tag ${*:2}" >> "$LOG"; exit 0 ;;
  run)
    name=""; port=""; prev=""
    for a in "$@"; do
      case "$prev" in
        --name) name="$a" ;;
        -p) port="$(echo "$a" | sed -E 's/.*:([0-9]+):[0-9]+$/\1/')" ;;
      esac
      prev="$a"
    done
    echo "run --name $name -p $port :: $*" >> "$LOG"
    [ -n "$name" ] && add_ct "$name" "$port"
    [ -n "$port" ] && [ ! -f "$DS/health-$port" ] && printf '{"in_flight":0}' > "$DS/health-$port"
    exit 0 ;;
  ps)
    if printf ' %s ' "$@" | grep -q ' -a '; then names_all; else names_running; fi
    exit 0 ;;
  port) echo "127.0.0.1:$(port_of "$2")"; exit 0 ;;
  inspect)
    fmt=""; prev=""
    for a in "$@"; do case "$prev" in -f) fmt="$a" ;; esac; prev="$a"; done
    nm="${*: -1}"
    case "$fmt" in
      *State.Running*) [ "$(state_of "$nm")" = "running" ] && echo "true" || echo "false" ;;
      *.Id*) if [ "$nm" = "compose-gerrit-1" ]; then cat "$DS/gerrit-id"; else echo "id-$nm"; fi ;;
      *) echo "" ;;
    esac
    exit 0 ;;
  stop)
    nm="${*: -1}"; echo "stop $nm" >> "$LOG"
    # The graceful retire is backgrounded so the tick never waits on drain. If that subshell
    # leaks the deploy flock FD (9), it holds the lock for MCP_STOP_GRACE (up to 1260s) after
    # the tick exits, so every subsequent autodeploy tick skips. `docker stop` is only ever
    # reached via that backgrounded subshell, so FD 9 must be CLOSED here.
    if { true >&9; } 2>/dev/null; then echo "stop-fd9-leaked $nm" >> "$LOG"; fi
    set_state "$nm" exited; exit 0 ;;
  rm)
    if [ "$2" = "-f" ]; then nm="$3"; echo "rm-f $nm" >> "$LOG"
    else nm="$2"; echo "rm $nm" >> "$LOG"; fi
    del_ct "$nm"; exit 0 ;;
esac
exit 0
"""

_CURL_STUB = r"""
DS="__DSTATE__"
url="${*: -1}"
port="$(echo "$url" | sed -E 's#.*://[^/]*:([0-9]+)/.*#\1#')"
case "$url" in *://localhost/*|*://127.0.0.1/*) port=80 ;; esac
f="$DS/health-$port"
body="$(cat "$f" 2>/dev/null)" || exit 22
case "$body" in ""|DOWN) exit 22 ;; esac
printf '%s' "$body"
exit 0
"""

_NGINX_STUB = r"""
LOG="__CMDLOG__"
case "$1" in
  -t) echo "nginx-t" >> "$LOG"; exit __NGINX_T_RC__ ;;
  -s) echo "nginx-reload" >> "$LOG"; exit __NGINX_RELOAD_RC__ ;;
esac
exit 0
"""

# git stub for the container tests: `main` advanced and ONLY an mcp-exclusive path
# (Dockerfile.mcp) changed, so only the mcp block runs. The diff HONORS the pathspec so
# `changed "$BOT_PATHS"` (which lacks Dockerfile.mcp) stays false.
_GIT_STUB_MCP_ONLY = r"""
args=("$@"); sub=""
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[i]}" in -C) ((i++)) ;; -*) ;; *) sub="${args[i]}"; break ;; esac
done
case "$sub" in
  remote) echo "https://github.com/navapbc/rebar.git"; exit 0 ;;
  fetch) exit 0 ;;
  rev-parse) cat "__TARGET_FILE__"; exit 0 ;;
  checkout) exit 0 ;;
  diff) case "$*" in *Dockerfile.mcp*) echo "infra/compose/Dockerfile.mcp" ;; esac; exit 0 ;;
  *) exit 0 ;;
esac
"""

# git stub for the change-detection contrast: real diff/checkout against a REAL temp repo,
# only the network legs faked (no https fetch of a real remote).
_GIT_STUB_REAL_DELEGATE = r"""
REAL_GIT="__REAL_GIT__"
args=("$@"); sub=""
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[i]}" in -C) ((i++)) ;; -*) ;; *) sub="${args[i]}"; break ;; esac
done
case "$sub" in
  remote) echo "https://github.com/navapbc/rebar.git"; exit 0 ;;
  fetch) exit 0 ;;
  rev-parse)
    case "$*" in *origin/main*) cat "__TARGET_FILE__"; exit 0 ;; esac
    exec "$REAL_GIT" "$@" ;;
  *) exec "$REAL_GIT" "$@" ;;
esac
"""


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    p.chmod(0o755)


def _write_common_stubs(
    bin_dir: Path,
    cmd_log: Path,
    dstate: Path,
    *,
    nginx_t_rc: int = 0,
    nginx_reload_rc: int = 0,
) -> None:
    _stub(
        bin_dir,
        "docker",
        _DOCKER_STUB.replace("__CMDLOG__", str(cmd_log)).replace("__DSTATE__", str(dstate)),
    )
    _stub(bin_dir, "curl", _CURL_STUB.replace("__DSTATE__", str(dstate)))
    _stub(
        bin_dir,
        "nginx",
        _NGINX_STUB.replace("__CMDLOG__", str(cmd_log))
        .replace("__NGINX_T_RC__", str(nginx_t_rc))
        .replace("__NGINX_RELOAD_RC__", str(nginx_reload_rc)),
    )
    _stub(bin_dir, "flock", "exit 0")
    _stub(bin_dir, "timeout", 'shift; exec "$@"')
    for tool in ("rsync", "chown", "stat"):
        _stub(bin_dir, tool, "exit 0")


def _seed_container(
    dstate: Path, name: str, port: int, in_flight: int = 0, state: str = "running"
) -> None:
    ct = dstate / "containers"
    with ct.open("a") as fh:
        fh.write(f"{name}|{port}|{state}\n")
    (dstate / f"health-{port}").write_text(f'{{"in_flight":{in_flight}}}')


@pytest.fixture
def mcp_box(tmp_path: Path) -> dict[str, object]:
    """A fake box where `main` advanced with an mcp-only source change.

    One backend exists at boot: the compose-managed `compose-mcp-1` on host port 8091, with the
    nginx /mcp/ upstream include pointing there. The blue/green ports are 8092 (A) / 8093 (B).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    dstate = tmp_path / "dstate"
    dstate.mkdir()
    deploy_repo = tmp_path / "deploy"
    (deploy_repo / "infra" / "compose").mkdir(parents=True)
    (deploy_repo / "infra" / "scripts").mkdir(parents=True)
    (deploy_repo / "infra" / "compose" / ".env").write_text("PREEXISTING=1\n")
    # The mcp deploy path re-materializes its SSM-sourced secrets before starting the new
    # container (receptive-houndy-nilgai), so the fake box must carry the script the real box
    # has -- same stub the bot-path fixtures use. Without it the deploy aborts at
    # `mcp-secrets-fetch-failed` before any of the blue-green assertions are reached.
    (deploy_repo / "infra" / "scripts" / "fetch-secrets.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n"
    )
    (deploy_repo / "infra" / "scripts" / "fetch-secrets.sh").chmod(0o755)
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)

    cmd_log = tmp_path / "cmd-log"
    target_file = tmp_path / "target-sha"
    target_file.write_text(_TARGET + "\n")
    (state / "deployed-sha").write_text(_DEPLOYED + "\n")

    upstream = tmp_path / "mcp-upstream.conf"
    upstream.write_text("server 127.0.0.1:8091;\n")
    (dstate / "gerrit-id").write_text("gerrit-abc123\n")
    (dstate / "health-8000").write_text('{"in_flight":0}')
    _seed_container(dstate, "compose-mcp-1", 8091, in_flight=0)

    _write_common_stubs(bin_dir, cmd_log, dstate)
    _stub(bin_dir, "git", _GIT_STUB_MCP_ONLY.replace("__TARGET_FILE__", str(target_file)))

    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    env.update(
        {
            "STATE_DIR": str(state),
            "DEPLOY_REPO": str(deploy_repo),
            "COMPOSE_DIR": str(deploy_repo / "infra" / "compose"),
            "MIRROR_DIR": str(mirror),
            "MCP_UPSTREAM_FILE": str(upstream),
            "MCP_HEALTH_TIMEOUT": "4",
            "MCP_MEM_AVAILABLE_MB": "4096",
        }
    )
    return {
        "env": env,
        "bin_dir": bin_dir,
        "cmd_log": cmd_log,
        "state": state,
        "dstate": dstate,
        "upstream": upstream,
        "target_file": target_file,
    }


def _run(
    box: dict[str, object], extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(box["env"])  # type: ignore[arg-type]
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _commands(box: dict[str, object]) -> list[str]:
    cmd_log: Path = box["cmd_log"]  # type: ignore[assignment]
    return cmd_log.read_text().splitlines() if cmd_log.exists() else []


def _commands_eventually(box: dict[str, object], needle: str, timeout: float = 6.0) -> list[str]:
    """Poll the call log until ``needle`` appears — the graceful retire `docker stop` is issued
    in the BACKGROUND (so the tick never waits on drain), so its log line can land just after
    the tick returns."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        cmds = _commands(box)
        if any(needle in c for c in cmds):
            return cmds
        time.sleep(0.1)
    return _commands(box)


def _markers(result: subprocess.CompletedProcess[str], token: str) -> list[str]:
    journal = result.stdout + result.stderr
    return [ln for ln in journal.splitlines() if ln.startswith(token + " ")]


def _run_line(cmds: list[str]) -> str:
    for c in cmds:
        if c.startswith("run --name"):
            return c
    return ""


# --------------------------------------------------------------------------- #
# cutover ordering: start -> health -> flip -> retire, without waiting on drain #
# --------------------------------------------------------------------------- #


def test_cutover_starts_new_flips_then_retires_without_waiting(mcp_box: dict[str, object]) -> None:
    """The core blue-green cutover. A new container starts on a free port, its /health passes,
    the nginx upstream is atomically flipped to it, and only THEN is the old one retired — and
    the deploy returns success WITHOUT waiting on the old container's in-flight work."""
    # An in-flight op on the OLD backend must NOT hold the deploy back (that is the whole point
    # of blue-green vs the review-bot drain).
    (mcp_box["dstate"] / "health-8091").write_text('{"in_flight":5}')  # type: ignore[operator]
    result = _run(mcp_box)
    cmds = _commands_eventually(mcp_box, "stop compose-mcp-1")
    upstream: Path = mcp_box["upstream"]  # type: ignore[assignment]
    ctx = f"rc={result.returncode}\ncmds={cmds}\n{result.stdout}\n{result.stderr}"

    assert result.returncode == 0, f"a healthy blue-green cutover must succeed\n{ctx}"
    run_line = _run_line(cmds)
    assert "-p 8092" in run_line, (
        f"the NEW container must start on the free blue/green port 8092\n{ctx}"
    )
    assert "compose-build-mcp" in cmds, f"the immutable image must be built\n{ctx}"
    assert "nginx-reload" in cmds, f"the upstream must be flipped with an nginx reload\n{ctx}"
    assert upstream.read_text().strip() == "server 127.0.0.1:8092;", (
        f"the materialized include must now point at the new container's port\n{ctx}"
    )
    # ordering: build/run (start) < nginx-reload (flip) < stop (retire).
    idx_run = next(i for i, c in enumerate(cmds) if c.startswith("run --name"))
    idx_flip = next(i for i, c in enumerate(cmds) if c == "nginx-reload")
    idx_stop = next(i for i, c in enumerate(cmds) if c.startswith("stop "))
    assert idx_run < idx_flip < idx_stop, (
        f"cutover order must be start-new -> flip -> retire-old\n{ctx}"
    )


def test_cutover_does_not_force_kill_the_old_container(mcp_box: dict[str, object]) -> None:
    """Retirement is a GRACEFUL `docker stop` (SIGTERM -> the container's own bounded self-drain),
    never a `docker rm -f`/SIGKILL of a still-serving container."""
    (mcp_box["dstate"] / "health-8091").write_text('{"in_flight":3}')  # type: ignore[operator]
    result = _run(mcp_box)
    cmds = _commands_eventually(mcp_box, "stop compose-mcp-1")
    ctx = f"rc={result.returncode}\ncmds={cmds}\n{result.stdout}\n{result.stderr}"

    assert any(c.startswith("stop compose-mcp-1") for c in cmds), (
        f"the OLD container must be retired with a graceful `docker stop`\n{ctx}"
    )
    assert not any("rm-f compose-mcp-1" in c for c in cmds), (
        f"a serving OLD container must NEVER be `docker rm -f`/SIGKILLed — that kills its "
        f"in-flight certified op (the review-bot bug 7b4a this design avoids)\n{ctx}"
    )
    # the graceful stop must carry the full grace so Docker never escalates SIGTERM->SIGKILL.
    assert "--time 1260" in (result.stdout + result.stderr) or any(
        "--time 1260" in c for c in _commands_eventually(mcp_box, "--time 1260")
    ), f"the stop must use the container's full self-drain grace (1260s)\n{ctx}"


# --------------------------------------------------------------------------- #
# memory pre-check aborts BEFORE the 2x overlap                                #
# --------------------------------------------------------------------------- #


def test_low_memory_aborts_before_starting_a_second_container(mcp_box: dict[str, object]) -> None:
    """On the 8 GiB box a blue-green overlap doubles the MCP memory footprint. Below
    MCP_MEM_MIN_MB the deploy must ABORT before it ever starts the second container."""
    result = _run(mcp_box, {"MCP_MEM_AVAILABLE_MB": "512", "MCP_MEM_MIN_MB": "1024"})
    cmds = _commands(mcp_box)
    upstream: Path = mcp_box["upstream"]  # type: ignore[assignment]
    ctx = f"rc={result.returncode}\ncmds={cmds}\n{result.stdout}\n{result.stderr}"

    assert not any(c.startswith("run --name") for c in cmds), (
        f"no second container may be started when memory is below the floor\n{ctx}"
    )
    assert _markers(result, "AUTODEPLOY_MCP_MEM_ABORT"), (
        f"a memory abort must be COUNTABLE via its own marker\n{ctx}"
    )
    assert upstream.read_text().strip() == "server 127.0.0.1:8091;", (
        f"an aborted deploy must leave the upstream untouched\n{ctx}"
    )
    assert result.returncode != 0, f"the abort is a deploy failure so backoff/retry engage\n{ctx}"
    assert (mcp_box["state"] / "deployed-sha").read_text().strip() == _DEPLOYED, (  # type: ignore[operator]
        f"an aborted deploy must NOT advance deployed-sha\n{ctx}"
    )


def test_unreadable_memory_fails_open(mcp_box: dict[str, object]) -> None:
    """An unreadable memory reading must fail OPEN — a broken probe must not wedge deploys."""
    result = _run(mcp_box, {"MCP_MEM_AVAILABLE_MB": "", "MCP_MEM_MIN_MB": "1024"})
    cmds = _commands_eventually(mcp_box, "run --name")
    run_line = _run_line(cmds)
    upstream: Path = mcp_box["upstream"]  # type: ignore[assignment]
    ctx = f"rc={result.returncode}\ncmds={cmds}\n{result.stdout}\n{result.stderr}"
    # With no /proc/meminfo reading available in the test env, the override is empty and the
    # probe cannot read a value: it must proceed, not abort.
    assert not _markers(result, "AUTODEPLOY_MCP_MEM_ABORT"), (
        f"an unreadable memory signal must not abort the deploy (fail-open)\n{ctx}"
    )
    # Liveness anchor: assert the deploy actually REACHED and PASSED the memory gate rather
    # than silently no-opping — the new container is started and the cutover completes. Without
    # this, the fail-open oracle would pass even if the gate wrongly aborted for some other
    # reason (or the block never ran at all).
    assert "-p 8092" in run_line, (
        f"fail-open must proceed past the memory gate and start the NEW container on 8092\n{ctx}"
    )
    assert result.returncode == 0, (
        f"a fail-open deploy must complete the cutover successfully (exit 0)\n{ctx}"
    )
    assert upstream.read_text().strip() == "server 127.0.0.1:8092;", (
        f"fail-open must flip the /mcp upstream to the new container (cutover reached)\n{ctx}"
    )


# --------------------------------------------------------------------------- #
# port exhaustion: both blue/green ports busy -> no colliding 3rd container    #
# --------------------------------------------------------------------------- #


def test_both_ports_busy_does_not_start_a_third_container(mcp_box: dict[str, object]) -> None:
    """Managed ports are EXACTLY {8091, 8092, 8093}. If both blue/green ports are already held by
    un-reaped containers, the deploy must NOT `docker run -p` onto an occupied port; it emits the
    cap marker and backs off instead."""
    # Re-seed: the compose-original is gone; both A and B are held by draining containers.
    (mcp_box["dstate"] / "containers").write_text("")  # type: ignore[operator]
    _seed_container(mcp_box["dstate"], "rebar-mcp-old-a-8092", 8092, in_flight=2)  # type: ignore[arg-type]
    _seed_container(mcp_box["dstate"], "rebar-mcp-old-b-8093", 8093, in_flight=2)  # type: ignore[arg-type]
    (mcp_box["upstream"]).write_text("server 127.0.0.1:8092;\n")  # type: ignore[operator]

    result = _run(mcp_box)
    cmds = _commands(mcp_box)
    ctx = f"rc={result.returncode}\ncmds={cmds}\n{result.stdout}\n{result.stderr}"

    assert not any(c.startswith("run --name") for c in cmds), (
        f"no 3rd container may start when both blue/green ports are occupied\n{ctx}"
    )
    assert not any("rm-f" in c for c in cmds), (
        f"port exhaustion must never resolve by force-killing a live container\n{ctx}"
    )
    assert _markers(result, "AUTODEPLOY_MCP_RETIRE_CAP"), (
        f"exceeding the managed-container cap must emit the cap marker\n{ctx}"
    )
    assert result.returncode != 0, f"port exhaustion backs off and retries\n{ctx}"


# --------------------------------------------------------------------------- #
# health / nginx failure rolls back (old include byte-identical)              #
# --------------------------------------------------------------------------- #


def test_unhealthy_new_container_leaves_old_upstream_current(mcp_box: dict[str, object]) -> None:
    """If the NEW container never passes /health, the old upstream must remain live and
    byte-identical, the new container removed, and a retry next healthy tick succeeds."""
    upstream: Path = mcp_box["upstream"]  # type: ignore[assignment]
    before = upstream.read_bytes()
    # Pre-mark the new port's health DOWN so the readiness gate fails.
    (mcp_box["dstate"] / "health-8092").write_text("DOWN")  # type: ignore[operator]

    result = _run(mcp_box)
    cmds = _commands(mcp_box)
    ctx = f"rc={result.returncode}\ncmds={cmds}\n{result.stdout}\n{result.stderr}"

    assert upstream.read_bytes() == before, (
        f"a failed health check must leave the OLD include byte-identical\n{ctx}"
    )
    assert "nginx-reload" not in cmds, (
        f"the upstream must not be flipped to an unhealthy container\n{ctx}"
    )
    assert any("rm-f" in c for c in cmds), (
        f"the failed NEW container (never in the upstream) must be removed\n{ctx}"
    )
    assert _markers(result, "AUTODEPLOY_ERROR"), (
        f"an unhealthy deploy is an AUTODEPLOY_ERROR\n{ctx}"
    )
    assert (
        result.returncode != 0
        and (
            mcp_box["state"] / "deployed-sha"  # type: ignore[operator]
        )
        .read_text()
        .strip()
        == _DEPLOYED
    ), f"a failed deploy must not advance deployed-sha\n{ctx}"

    # Retry on a healthy tick flips it (the SHA-keyed backoff window has since elapsed).
    (mcp_box["dstate"] / "health-8092").write_text('{"in_flight":0}')  # type: ignore[operator]
    (mcp_box["cmd_log"]).write_text("")  # type: ignore[operator]
    (mcp_box["state"] / "deploy-backoff").unlink(missing_ok=True)  # type: ignore[operator]
    result2 = _run(mcp_box)
    ctx2 = f"rc={result2.returncode}\n{result2.stdout}\n{result2.stderr}"
    assert result2.returncode == 0, f"a healthy retry must succeed\n{ctx2}"
    assert upstream.read_text().strip() == "server 127.0.0.1:8092;", (
        f"the healthy retry must flip the upstream to the new container\n{ctx2}"
    )


def test_secrets_fetch_failure_aborts_before_touching_the_live_upstream(
    mcp_box: dict[str, object],
) -> None:
    """An SSM failure must abort the mcp deploy with the OLD container still serving.

    The mcp path re-materializes its SSM-sourced secrets before starting the replacement
    container (receptive-houndy-nilgai: three PATs were rotated into SSM, nothing regenerated
    the on-disk tokens file, and every new container failed closed on the stale copy). This
    pins the ORDERING that makes that safe: if the refresh fails, the deploy stops while the
    old container is still live, rather than replacing a working container using stale
    secrets or leaving the endpoint down.
    """
    deploy_repo = Path(mcp_box["env"]["DEPLOY_REPO"])  # type: ignore[index]
    upstream: Path = mcp_box["upstream"]  # type: ignore[assignment]
    before = upstream.read_bytes()

    # Make the refresh fail the way an unreachable SSM would.
    fetch = deploy_repo / "infra" / "scripts" / "fetch-secrets.sh"
    fetch.write_text("#!/usr/bin/env bash\nexit 1\n")
    fetch.chmod(0o755)

    result = _run(mcp_box)
    cmds = _commands(mcp_box)
    ctx = f"rc={result.returncode}\ncmds={cmds}\n{result.stdout}\n{result.stderr}"

    assert upstream.read_bytes() == before, (
        f"a secrets-refresh failure must leave the OLD include byte-identical\n{ctx}"
    )
    assert "nginx-reload" not in cmds, (
        f"the upstream must never be flipped when the secrets refresh failed\n{ctx}"
    )
    assert "mcp-secrets-fetch-failed" in result.stdout + result.stderr, (
        f"the abort must name the secrets refresh, not masquerade as a generic failure\n{ctx}"
    )
    assert not any("run-d" in c for c in cmds), (
        f"no replacement container may be started against unrefreshed secrets\n{ctx}"
    )


def test_nginx_reload_failure_restores_the_previous_upstream(tmp_path: Path) -> None:
    """If `nginx -s reload` fails the flip, the previous include must be restored byte-identical
    and the new container removed."""
    # Build a box with an nginx stub whose reload fails.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    dstate = tmp_path / "dstate"
    dstate.mkdir()
    deploy_repo = tmp_path / "deploy"
    (deploy_repo / "infra" / "compose").mkdir(parents=True)
    (deploy_repo / "infra" / "scripts").mkdir(parents=True)
    (deploy_repo / "infra" / "compose" / ".env").write_text("X=1\n")
    # Same reason as the mcp_box fixture: the deploy path re-materializes SSM secrets first.
    (deploy_repo / "infra" / "scripts" / "fetch-secrets.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n"
    )
    (deploy_repo / "infra" / "scripts" / "fetch-secrets.sh").chmod(0o755)
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)
    cmd_log = tmp_path / "cmd-log"
    target_file = tmp_path / "target-sha"
    target_file.write_text(_TARGET + "\n")
    (state / "deployed-sha").write_text(_DEPLOYED + "\n")
    upstream = tmp_path / "mcp-upstream.conf"
    upstream.write_text("server 127.0.0.1:8091;\n")
    before = upstream.read_bytes()
    (dstate / "gerrit-id").write_text("gid\n")
    _seed_container(dstate, "compose-mcp-1", 8091, in_flight=0)
    _write_common_stubs(bin_dir, cmd_log, dstate, nginx_reload_rc=1)
    _stub(bin_dir, "git", _GIT_STUB_MCP_ONLY.replace("__TARGET_FILE__", str(target_file)))

    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    env.update(
        {
            "STATE_DIR": str(state),
            "DEPLOY_REPO": str(deploy_repo),
            "COMPOSE_DIR": str(deploy_repo / "infra" / "compose"),
            "MIRROR_DIR": str(mirror),
            "MCP_UPSTREAM_FILE": str(upstream),
            "MCP_HEALTH_TIMEOUT": "4",
            "MCP_MEM_AVAILABLE_MB": "4096",
        }
    )
    result = subprocess.run(
        ["bash", str(AUTODEPLOY)], env=env, capture_output=True, text=True, timeout=120, check=False
    )
    cmds = cmd_log.read_text().splitlines() if cmd_log.exists() else []
    ctx = f"rc={result.returncode}\ncmds={cmds}\n{result.stdout}\n{result.stderr}"
    assert upstream.read_bytes() == before, (
        f"a failed nginx reload must restore the previous upstream byte-identical\n{ctx}"
    )
    assert any("rm-f" in c for c in cmds), f"the un-adopted NEW container must be removed\n{ctx}"
    assert result.returncode != 0, f"a failed flip must back off\n{ctx}"


# --------------------------------------------------------------------------- #
# blast radius: the gerrit container is never touched                         #
# --------------------------------------------------------------------------- #


def test_gerrit_container_id_unchanged_across_mcp_deploy(mcp_box: dict[str, object]) -> None:
    result = _run(mcp_box)
    ctx = f"rc={result.returncode}\n{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, ctx
    assert not any(
        ln.startswith("AUTODEPLOY_ERROR ") and "blast-radius" in ln
        for ln in (result.stdout + result.stderr).splitlines()
    ), f"an mcp deploy must not change the gerrit container (bounded blast radius)\n{ctx}"


# --------------------------------------------------------------------------- #
# graceful retire on the up-to-date (no-op) tick                              #
# --------------------------------------------------------------------------- #


def test_no_op_tick_reaps_a_drained_old_container(mcp_box: dict[str, object]) -> None:
    """`autodeploy.sh` exits early when up-to-date, so the retire sweep must ALSO run there to
    reap containers that have finished draining since the flip."""
    # Up to date: deployed == target. An old container has drained (exited) on a non-live port.
    (mcp_box["state"] / "deployed-sha").write_text(_TARGET + "\n")  # type: ignore[operator]
    (mcp_box["dstate"] / "containers").write_text("")  # type: ignore[operator]
    _seed_container(mcp_box["dstate"], "rebar-mcp-live-8092", 8092, in_flight=0)  # type: ignore[arg-type]
    _seed_container(  # type: ignore[arg-type]
        mcp_box["dstate"], "rebar-mcp-old-8091", 8091, in_flight=0, state="exited"
    )
    (mcp_box["upstream"]).write_text("server 127.0.0.1:8092;\n")  # type: ignore[operator]

    result = _run(mcp_box)
    cmds = _commands_eventually(mcp_box, "rm rebar-mcp-old-8091")
    ctx = f"rc={result.returncode}\ncmds={cmds}\n{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, f"a no-op tick is a normal outcome\n{ctx}"
    assert any("rm rebar-mcp-old-8091" in c for c in cmds), (
        f"a drained (exited) old container must be reaped on the no-op tick\n{ctx}"
    )
    assert not any(
        "rm rebar-mcp-live-8092" in c or "rm-f rebar-mcp-live-8092" in c for c in cmds
    ), f"the live container must never be reaped\n{ctx}"


def test_retire_never_stops_running_containers_when_live_port_unknown(
    mcp_box: dict[str, object],
) -> None:
    """If the /mcp upstream include is missing/unreadable the live port is UNKNOWN. The retire
    sweep must then fail SAFE — it may reap already-EXITED containers, but it must NOT `docker
    stop` any RUNNING managed container, because it cannot prove which one is still serving
    /mcp. Stopping them all would kill the live backend, the exact guarantee this target exists
    to preserve (bug 7b4a)."""
    (mcp_box["state"] / "deployed-sha").write_text(_TARGET + "\n")  # type: ignore[operator]
    (mcp_box["dstate"] / "containers").write_text("")  # type: ignore[operator]
    _seed_container(mcp_box["dstate"], "rebar-mcp-a-8092", 8092, in_flight=0)  # type: ignore[arg-type]
    _seed_container(mcp_box["dstate"], "compose-mcp-1", 8091, in_flight=0)  # type: ignore[arg-type]
    # The upstream include is unreadable -> mcp_live_port yields empty (no live port known).
    (mcp_box["upstream"]).write_text("")  # type: ignore[operator]

    result = _run(mcp_box)
    cmds = _commands(mcp_box)
    ctx = f"rc={result.returncode}\ncmds={cmds}\n{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, f"a no-op tick is a normal outcome\n{ctx}"
    assert not any(c.startswith("stop ") for c in cmds), (
        f"with the live port UNKNOWN the sweep must not stop ANY running container "
        f"(fail-safe: it could be the live backend)\n{ctx}"
    )


def test_backgrounded_retire_does_not_hold_the_deploy_lock(mcp_box: dict[str, object]) -> None:
    """The graceful retire `docker stop` is backgrounded so the tick never waits on drain. That
    subshell must NOT inherit the deploy flock FD (9): if it did, it would hold the lock for up
    to MCP_STOP_GRACE (1260s) after the tick exits, so every subsequent autodeploy tick — for
    ANY component, not just mcp — would skip with 'another deploy holds the lock'. The docker
    stub records `stop-fd9-leaked` iff FD 9 is still open when `docker stop` runs."""
    result = _run(mcp_box)
    cmds = _commands_eventually(mcp_box, "stop compose-mcp-1")
    ctx = f"rc={result.returncode}\ncmds={cmds}\n{result.stdout}\n{result.stderr}"
    assert any(c.startswith("stop ") for c in cmds), (
        f"the retire must issue a graceful docker stop\n{ctx}"
    )
    assert not any("stop-fd9-leaked" in c for c in cmds), (
        f"the backgrounded retire must CLOSE the deploy flock FD (9), or it wedges the "
        f"whole autodeploy timer for up to MCP_STOP_GRACE seconds\n{ctx}"
    )


def test_over_cap_managed_set_emits_retire_cap_without_forcing(mcp_box: dict[str, object]) -> None:
    """When the managed set exceeds MCP_RELEASES_CAP the sweep emits AUTODEPLOY_MCP_RETIRE_CAP
    (reason 'over-cap') and forces NO kill — distinct from the port-exhaustion path (same marker,
    reason 'port-exhausted'). This exercises the over-cap branch of mcp_retire_sweep on the no-op
    tick with MORE managed containers than the cap allows, none reapable (all still running)."""
    (mcp_box["state"] / "deployed-sha").write_text(_TARGET + "\n")  # type: ignore[operator]
    (mcp_box["dstate"] / "containers").write_text("")  # type: ignore[operator]
    # Live backend on 8092 plus THREE still-draining olds -> 4 managed > cap (3), none exited.
    _seed_container(mcp_box["dstate"], "rebar-mcp-live-8092", 8092, in_flight=0)  # type: ignore[arg-type]
    _seed_container(mcp_box["dstate"], "rebar-mcp-old-8091", 8091, in_flight=3)  # type: ignore[arg-type]
    _seed_container(mcp_box["dstate"], "rebar-mcp-old-8093", 8093, in_flight=3)  # type: ignore[arg-type]
    _seed_container(mcp_box["dstate"], "compose-mcp-1", 8090, in_flight=3)  # type: ignore[arg-type]
    (mcp_box["upstream"]).write_text("server 127.0.0.1:8092;\n")  # type: ignore[operator]

    result = _run(mcp_box, {"MCP_RELEASES_CAP": "3"})
    cmds = _commands(mcp_box)
    markers = _markers(result, "AUTODEPLOY_MCP_RETIRE_CAP")
    ctx = (
        f"rc={result.returncode}\ncmds={cmds}\nmarkers={markers}\n{result.stdout}\n{result.stderr}"
    )
    assert result.returncode == 0, f"a no-op tick is a normal outcome\n{ctx}"
    assert any('"reason": "over-cap"' in m for m in markers), (
        f"over-cap must emit AUTODEPLOY_MCP_RETIRE_CAP with reason over-cap\n{ctx}"
    )
    assert not any(c.startswith("rm-f ") for c in cmds), (
        f"over-cap must NOT force-kill any container\n{ctx}"
    )


# --------------------------------------------------------------------------- #
# compose parity: docker-run env/mounts reproduce the compose `mcp:` service   #
# --------------------------------------------------------------------------- #


def _parse_compose_mcp_env_and_volumes() -> tuple[dict[str, str], list[str]]:
    """Parse the `mcp:` service `environment:` mapping and `volumes:` from docker-compose.yml,
    resolving compose ``${VAR:-default}`` to its default (docker run does NOT interpolate)."""
    text = COMPOSE_FILE.read_text().splitlines()
    # locate the mcp service block (2-space indented key under services).
    start = None
    for i, ln in enumerate(text):
        if re.match(r"^  mcp:\s*$", ln):
            start = i
            break
    assert start is not None, "mcp service not found in docker-compose.yml"
    end = len(text)
    for j in range(start + 1, len(text)):
        if re.match(r"^  \S", text[j]) or re.match(r"^\S", text[j]):
            end = j
            break
    block = text[start:end]

    env: dict[str, str] = {}
    volumes: list[str] = []
    mode = None
    for ln in block:
        if re.match(r"^    environment:\s*$", ln):
            mode = "env"
            continue
        if re.match(r"^    volumes:\s*$", ln):
            mode = "vol"
            continue
        if re.match(r"^    \S", ln):  # a new 4-space key ends the current sub-block
            mode = None
        if mode == "env":
            m = re.match(r"^      ([A-Z0-9_]+):\s*(.+?)\s*$", ln)
            if m:
                key, raw = m.group(1), m.group(2)
                raw = raw.strip().strip('"').strip("'")
                dm = re.match(r"^\$\{[^:}]+:-(.*)\}$", raw)
                env[key] = dm.group(1) if dm else raw
        elif mode == "vol":
            m = re.match(r"^      -\s*(.+?)\s*$", ln)
            if m:
                volumes.append(m.group(1).strip().strip('"').strip("'"))
    return env, volumes


# Compose env keys that are carried into the blue-green container by `--env-file` alone.
# Their compose spelling is `${KEY:-}` — compose resolves that from the project-dir .env,
# but a bare `docker run` does NOT, so re-spelling them as `-e` would overwrite the real
# value with an empty string.
_ENV_FILE_ONLY = frozenset({"MCP_TICKETS_PAT"})


def test_docker_run_matches_compose_mcp_service(mcp_box: dict[str, object]) -> None:
    """`mcp_run_new` must reproduce the compose `mcp:` service env/mounts EXACTLY. /health is
    auth-independent (mounted outside the auth middleware), so it cannot catch a wrong
    REBAR_MCP_AUTH_*/ALLOWED_HOSTS — only this static parity oracle can."""
    env, volumes = _parse_compose_mcp_env_and_volumes()
    result = _run(mcp_box)
    cmds = _commands_eventually(mcp_box, "run --name")
    run_line = _run_line(cmds)
    ctx = f"rc={result.returncode}\nrun_line={run_line}\n{result.stdout}\n{result.stderr}"
    assert run_line, f"the mcp deploy must `docker run` a new container\n{ctx}"
    # Anti-vacuity floor: the per-key/per-volume loops below assert NOTHING if the compose
    # parse yielded empty collections (a silently-broken regex would make this test always
    # pass). Pin that the parse actually recovered the mcp service's env + both secret mounts.
    assert len(env) >= 5, (
        f"compose parse must recover the mcp service env (got {len(env)} keys: {sorted(env)})"
    )
    assert {"REBAR_MCP_HTTP_PORT", "REBAR_MCP_HTTP_HOST"} <= env.keys(), (
        f"compose parse must include the core REBAR_MCP_* keys (got {sorted(env)})"
    )
    assert len(volumes) >= 2, f"compose parse must recover both :ro secret mounts (got {volumes})"

    for key, value in env.items():
        if key in _ENV_FILE_ONLY:
            # Deliberately NOT spelled as `-e` here: its value lives only in the
            # SSM-materialized .env, and the deploy shell does not interpolate from that
            # file, so a `-e KEY=${KEY:-}` would resolve EMPTY and CLOBBER the .env value.
            # `--env-file` (asserted below) is the carrier; pin the exclusion so a future
            # edit that adds the flag has to come here and re-reason about it.
            assert f"-e {key}=" not in run_line, (
                f"`{key}` must reach the container via --env-file, not `-e` (an `-e` with an "
                f"un-interpolated compose default would blank the .env value)\n{ctx}"
            )
            continue
        assert f"-e {key}={value}" in run_line, (
            f"docker run must carry compose env `{key}={value}`\n{ctx}"
        )
    # The secret mounts, read-only, by their in-container path; plus the persistent named
    # data volumes (no :ro suffix) by their `name:path` pair.
    for vol in volumes:
        parts = vol.split(":")
        if parts[-1] == "ro":
            container_path = parts[-2]
            assert f"{container_path}:ro" in run_line, (
                f"docker run must bind-mount `{container_path}` read-only (compose parity)\n{ctx}"
            )
        else:
            assert len(parts) == 2, f"unrecognised compose mcp volume spec: {vol}"
            assert vol in run_line, (
                f"docker run must mount the named volume `{vol}` (compose parity)\n{ctx}"
            )
    assert "--env-file" in run_line and ".env" in run_line, (
        f"docker run must load the .env env-file\n{ctx}"
    )
    assert "--restart always" in run_line, (
        f"docker run must set restart:always (compose parity)\n{ctx}"
    )
    assert "--stop-timeout 1260" in run_line, (
        f"docker run must set the 1260s stop-timeout so Docker never SIGKILLs mid-drain\n{ctx}"
    )


# --------------------------------------------------------------------------- #
# change-detection contrast: REAL temp git repo, two commits                  #
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _make_repo(root: Path, touched: list[str]) -> tuple[Path, str, str]:
    """A real git repo with a baseline commit and a second commit that modifies ``touched``.
    Returns (repo, deployed_sha, target_sha)."""
    repo = root
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    baseline = [
        "infra/compose/Dockerfile.mcp",
        "infra/compose/Dockerfile.reviewbot",
        "infra/compose/docker-compose.yml",
        "src/rebar/app.py",
        "pyproject.toml",
        "uv.lock",
        "infra/scripts/fetch-secrets.sh",
    ]
    for rel in baseline:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("v1\n")
    (repo / "infra" / "scripts" / "fetch-secrets.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    deployed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    for rel in touched:
        (repo / rel).write_text("v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    target = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, deployed, target


def _real_git_box(tmp_path: Path, touched: list[str]) -> dict[str, object]:
    import shutil

    real_git = shutil.which("git")
    assert real_git, "git must be installed for the change-detection test"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    dstate = tmp_path / "dstate"
    dstate.mkdir()
    mirror, deployed, target = _make_repo(tmp_path / "mirror", touched)
    deploy_repo = tmp_path / "deploy"
    (deploy_repo / "infra" / "compose").mkdir(parents=True)
    (deploy_repo / "infra" / "scripts").mkdir(parents=True)
    (deploy_repo / "infra" / "compose" / ".env").write_text("X=1\n")
    (deploy_repo / "infra" / "scripts" / "fetch-secrets.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n"
    )

    cmd_log = tmp_path / "cmd-log"
    target_file = tmp_path / "target-sha"
    target_file.write_text(target + "\n")
    (state / "deployed-sha").write_text(deployed + "\n")
    upstream = tmp_path / "mcp-upstream.conf"
    upstream.write_text("server 127.0.0.1:8091;\n")
    (dstate / "gerrit-id").write_text("gid\n")
    (dstate / "health-8000").write_text('{"in_flight":0}')
    _seed_container(dstate, "compose-mcp-1", 8091, in_flight=0)

    _write_common_stubs(bin_dir, cmd_log, dstate)
    _stub(
        bin_dir,
        "git",
        _GIT_STUB_REAL_DELEGATE.replace("__REAL_GIT__", real_git).replace(
            "__TARGET_FILE__", str(target_file)
        ),
    )

    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    env.update(
        {
            "STATE_DIR": str(state),
            "DEPLOY_REPO": str(deploy_repo),
            "COMPOSE_DIR": str(deploy_repo / "infra" / "compose"),
            "MIRROR_DIR": str(mirror),
            "MCP_UPSTREAM_FILE": str(upstream),
            "MCP_HEALTH_TIMEOUT": "4",
            "MCP_MEM_AVAILABLE_MB": "4096",
            "HEALTH_TIMEOUT": "4",
        }
    )
    return {"env": env, "cmd_log": cmd_log}


def test_mcp_only_change_triggers_only_mcp(tmp_path: Path) -> None:
    box = _real_git_box(tmp_path, ["infra/compose/Dockerfile.mcp"])
    result = subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=box["env"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,  # type: ignore[arg-type]
    )
    cmds = (box["cmd_log"]).read_text().splitlines() if (box["cmd_log"]).exists() else []  # type: ignore[operator]
    ctx = f"rc={result.returncode}\ncmds={cmds}\n{result.stdout}\n{result.stderr}"
    assert any("compose-build-mcp" in c for c in cmds), f"an mcp-only change must build mcp\n{ctx}"
    assert not any("compose-build-review-bot" in c for c in cmds), (
        f"an mcp-only change must NOT rebuild the review-bot\n{ctx}"
    )


def test_reviewbot_only_change_triggers_only_reviewbot(tmp_path: Path) -> None:
    box = _real_git_box(tmp_path, ["infra/compose/Dockerfile.reviewbot"])
    result = subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=box["env"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,  # type: ignore[arg-type]
    )
    cmds = (box["cmd_log"]).read_text().splitlines() if (box["cmd_log"]).exists() else []  # type: ignore[operator]
    ctx = f"rc={result.returncode}\ncmds={cmds}\n{result.stdout}\n{result.stderr}"
    assert any("compose-build-review-bot" in c for c in cmds), (
        f"a bot-only change must build the bot\n{ctx}"
    )
    assert not any("compose-build-mcp" in c for c in cmds), (
        f"a review-bot-only change must NOT build mcp\n{ctx}"
    )


def test_shared_src_change_triggers_both_targets(tmp_path: Path) -> None:
    box = _real_git_box(tmp_path, ["src/rebar/app.py"])
    result = subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=box["env"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,  # type: ignore[arg-type]
    )
    cmds = (box["cmd_log"]).read_text().splitlines() if (box["cmd_log"]).exists() else []  # type: ignore[operator]
    ctx = f"rc={result.returncode}\ncmds={cmds}\n{result.stdout}\n{result.stderr}"
    assert any("compose-build-review-bot" in c for c in cmds), (
        f"a shared src/rebar change must build the review-bot\n{ctx}"
    )
    assert any("compose-build-mcp" in c for c in cmds), (
        f"a shared src/rebar change must ALSO build mcp — independently\n{ctx}"
    )
