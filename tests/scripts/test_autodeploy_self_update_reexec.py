"""autodeploy applies a same-tick change to its OWN container-constructing body.

Root cause (bug 2d65-32dd-28b9-43ef): within one tick autodeploy rsyncs the mirror over
``$DEPLOY_REPO`` — which on the box overwrites this very script's file — and THEN performs the
component rolls. bash keeps executing the body it already parsed, so a commit that edits a
container-constructing function (``mcp_run_new``, whose inline ``docker run`` argv is the mcp
container's entire construction) builds the container from the STALE body while every marker
and log line reports success. No later tick corrects it, because the component marker was
already stamped to the target.

These are real-subprocess integration tests, in the house style of
``test_autodeploy_mcp_bluegreen.py``: the box's binaries are stubbed onto PATH and the shipped
``infra/scripts/autodeploy.sh`` runs. The one departure the defect REQUIRES is that the script
is executed from the ``$DEPLOY_REPO`` copy (as systemd runs it on the box) and the ``rsync``
stub genuinely replaces that copy's ``autodeploy.sh`` from the mirror — so the running file is
overwritten mid-tick exactly as in production. The container's construction is asserted on the
constructed ``docker run`` argv captured in the call log, which is hermetic and needs no live
Docker daemon.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTODEPLOY = REPO_ROOT / "infra" / "scripts" / "autodeploy.sh"

_DEPLOYED = "d" * 40
_TARGET = "e" * 40

# The marker the mirror's (new) mcp_run_new body adds to the container's argv. A container built
# from the OLD parsed body will NOT carry it; a container built from the NEW body will.
_BODY_MARKER = "REBAR_BODY_MARKER=applied"
_ANCHOR = "    --label rebar.service=mcp \\\n"
_INJECT = _ANCHOR + f"    -e {_BODY_MARKER} \\\n"
_BOT_BODY_MARKER = "review-bot body marker applied"
_BOT_ANCHOR = (
    '  if ! ( cd "$COMPOSE_DIR" && docker compose build "$BOT_SERVICE" && '
    'docker compose up -d "$BOT_SERVICE" ); then\n'
)
_BOT_INJECT = f'  log "{_BOT_BODY_MARKER}"\n' + _BOT_ANCHOR


# --------------------------------------------------------------------------- #
# stubs                                                                        #
# --------------------------------------------------------------------------- #

# Captures the full `docker run` argv into the call log and models the minimum daemon behaviour
# the mcp blue-green path needs (ps/inspect/port and a health file for the new container).
_DOCKER_STUB = r"""
LOG="__CMDLOG__"
DS="__DSTATE__"
CT="$DS/containers"; touch "$CT"
port_of(){ awk -F'|' -v n="$1" '$1==n{print $2}' "$CT" | head -1; }
state_of(){ awk -F'|' -v n="$1" '$1==n{print $3}' "$CT" | head -1; }
add_ct(){ echo "$1|$2|running|$3" >> "$CT"; }
del_ct(){ grep -v "^$1|" "$CT" > "$CT.t" 2>/dev/null; mv "$CT.t" "$CT" 2>/dev/null || true; }
set_state(){ awk -F'|' -v n="$1" -v s="$2" 'BEGIN{OFS="|"}{if($1==n)$3=s;print}' "$CT" > "$CT.t"
  mv "$CT.t" "$CT"; }
names_running(){ awk -F'|' '$3=="running"{print $1}' "$CT"; }
names_all(){ awk -F'|' '{print $1}' "$CT"; }
case "$1 $2" in
  "compose build") echo "compose-build-$3" >> "$LOG"; exit 0 ;;
  "compose up")
    last=""; for a in "$@"; do last="$a"; done; echo "compose-up-$last" >> "$LOG"; exit 0 ;;
  "image inspect") exit 0 ;;
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
    [ -n "$name" ] && add_ct "$name" "$port" "${*: -1}"
    [ -n "$port" ] && [ ! -f "$DS/health-$port" ] && printf '{"in_flight":0}' > "$DS/health-$port"
    exit 0 ;;
  ps) if printf ' %s ' "$@" | grep -q ' -a '; then names_all; else names_running; fi; exit 0 ;;
  port) [ "$(state_of "$2")" = "running" ] || exit 1; echo "127.0.0.1:$(port_of "$2")"; exit 0 ;;
  inspect)
    fmt=""; prev=""; for a in "$@"; do case "$prev" in -f) fmt="$a" ;; esac; prev="$a"; done
    nm="${*: -1}"
    case "$fmt" in
      *State.Running*) [ "$(state_of "$nm")" = "running" ] && echo "true" || echo "false" ;;
      *HostConfig.PortBindings*) printf '{"8091/tcp":[{"HostPort":"%s"}]}\n' "$(port_of "$nm")" ;;
      *.Id*) if [ "$nm" = "compose-gerrit-1" ]; then cat "$DS/gerrit-id" 2>/dev/null
             else echo "id-$nm"; fi ;;
      *) echo "" ;;
    esac
    exit 0 ;;
  stop) echo "stop ${*: -1}" >> "$LOG"; set_state "${*: -1}" exited; exit 0 ;;
  rm) if [ "$2" = "-f" ]; then del_ct "$3"; else del_ct "$2"; fi; exit 0 ;;
esac
exit 0
"""

_CURL_STUB = r"""
DS="__DSTATE__"
url="${*: -1}"
port="$(echo "$url" | sed -E 's#.*://[^/]*:([0-9]+)/.*#\1#')"
case "$url" in *://localhost/*|*://127.0.0.1/*) port=80 ;; esac
body="$(cat "$DS/health-$port" 2>/dev/null)" || exit 22
case "$body" in ""|DOWN) exit 22 ;; esac
printf '%s' "$body"
exit 0
"""

_NGINX_STUB = r"""
LOG="__CMDLOG__"
case "$1" in
  -t) echo "nginx-t" >> "$LOG"; exit 0 ;;
  -s) echo "nginx-reload" >> "$LOG"; exit 0 ;;
esac
exit 0
"""

# rsync model: the defect is precisely that the source sync overwrites the RUNNING script. So the
# stub copies the mirror's autodeploy.sh over the deploy copy (the file being executed), matching
# `rsync -a --delete <excludes> "$MIRROR/" "$DEPLOY/"` (src/dst are the last two args).
_RSYNC_STUB = r"""
args=("$@"); n=${#args[@]}
src="${args[$((n-2))]}"; dst="${args[$((n-1))]}"
cp "${src}infra/scripts/autodeploy.sh" "${dst}infra/scripts/autodeploy.sh" 2>/dev/null || true
exit 0
"""

# git model: `main` advanced; report exactly the configured changed path when the pathspec covers
# it, so tests can drive the bot and mcp component branches independently.
_GIT_STUB = r"""
args=("$@"); sub=""
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[i]}" in -C) ((i++)) ;; -*) ;; *) sub="${args[i]}"; break ;; esac
done
case "$sub" in
  remote) echo "https://github.com/navapbc/rebar.git"; exit 0 ;;
  fetch) exit 0 ;;
  rev-parse) cat "__TARGET_FILE__"; exit 0 ;;
  checkout) exit 0 ;;
  diff)
    case "$*" in *"__CHANGED_PATH__"*) echo "__CHANGED_PATH__" ;; esac
    exit 0 ;;
  *) exit 0 ;;
esac
"""


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    p.chmod(0o755)


def _install_script(dst: Path, *, with_mcp_marker: bool, with_bot_marker: bool) -> None:
    text = AUTODEPLOY.read_text()
    if with_mcp_marker:
        assert _ANCHOR in text, "mcp_run_new anchor line moved; update _ANCHOR"
        text = text.replace(_ANCHOR, _INJECT, 1)
    if with_bot_marker:
        assert _BOT_ANCHOR in text, "review-bot deploy anchor moved; update _BOT_ANCHOR"
        text = text.replace(_BOT_ANCHOR, _BOT_INJECT, 1)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text)
    dst.chmod(0o755)


def _make_box(
    tmp_path: Path,
    *,
    changed_path: str,
    with_mcp_marker: bool,
    with_bot_marker: bool,
    fail_mktemp: bool = False,
) -> dict[str, object]:
    """A fake box whose deploy copy runs the OLD mcp_run_new body while the mirror carries the
    NEW body. Executing the deploy copy and letting the rsync stub replace it reproduces the
    same-tick self-update.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    dstate = tmp_path / "dstate"
    dstate.mkdir()
    deploy = tmp_path / "deploy"
    (deploy / "infra" / "compose").mkdir(parents=True)
    (deploy / "infra" / "scripts").mkdir(parents=True)
    (deploy / "infra" / "compose" / ".env").write_text("PREEXISTING=1\n")
    fetch = deploy / "infra" / "scripts" / "fetch-secrets.sh"
    fetch.write_text("#!/usr/bin/env bash\nexit 0\n")
    fetch.chmod(0o755)
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)

    # deploy copy = OLD body (no marker); mirror = NEW body (marker in mcp_run_new).
    installed = deploy / "infra" / "scripts" / "autodeploy.sh"
    _install_script(installed, with_mcp_marker=False, with_bot_marker=False)
    _install_script(
        mirror / "infra" / "scripts" / "autodeploy.sh",
        with_mcp_marker=with_mcp_marker,
        with_bot_marker=with_bot_marker,
    )

    cmd_log = tmp_path / "cmd-log"
    target_file = tmp_path / "target-sha"
    target_file.write_text(_TARGET + "\n")
    (state / "deployed-sha").write_text(_DEPLOYED + "\n")

    upstream = tmp_path / "mcp-upstream.conf"
    upstream.write_text("server 127.0.0.1:8091;\n")
    (dstate / "gerrit-id").write_text("gerrit-abc123\n")
    (dstate / "health-8000").write_text('{"in_flight":0}')
    # the boot backend on 8091 so the free blue/green port is 8092
    (dstate / "containers").write_text("compose-mcp-1|8091|running|\n")
    (dstate / "health-8091").write_text('{"in_flight":0}')

    _stub(
        bin_dir,
        "docker",
        _DOCKER_STUB.replace("__CMDLOG__", str(cmd_log)).replace("__DSTATE__", str(dstate)),
    )
    _stub(bin_dir, "curl", _CURL_STUB.replace("__DSTATE__", str(dstate)))
    _stub(bin_dir, "nginx", _NGINX_STUB.replace("__CMDLOG__", str(cmd_log)))
    _stub(bin_dir, "rsync", _RSYNC_STUB)
    _stub(
        bin_dir,
        "git",
        _GIT_STUB.replace("__TARGET_FILE__", str(target_file)).replace(
            "__CHANGED_PATH__", changed_path
        ),
    )
    _stub(bin_dir, "flock", "exit 0")
    _stub(bin_dir, "timeout", 'shift; exec "$@"')
    if fail_mktemp:
        _stub(bin_dir, "mktemp", "exit 1")
    for tool in ("chown", "stat"):
        _stub(bin_dir, tool, "exit 0")

    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    env.update(
        {
            "STATE_DIR": str(state),
            "DEPLOY_REPO": str(deploy),
            "COMPOSE_DIR": str(deploy / "infra" / "compose"),
            "MIRROR_DIR": str(mirror),
            "MCP_UPSTREAM_FILE": str(upstream),
            "MCP_HEALTH_TIMEOUT": "6",
            "MCP_MEM_AVAILABLE_MB": "4096",
        }
    )
    return {"env": env, "installed": installed, "cmd_log": cmd_log, "state": state}


@pytest.fixture
def box(tmp_path: Path) -> dict[str, object]:
    return _make_box(
        tmp_path,
        changed_path="infra/compose/Dockerfile.mcp",
        with_mcp_marker=True,
        with_bot_marker=False,
    )


@pytest.fixture
def bot_box(tmp_path: Path) -> dict[str, object]:
    return _make_box(
        tmp_path,
        changed_path="infra/compose/Dockerfile.reviewbot",
        with_mcp_marker=False,
        with_bot_marker=True,
    )


@pytest.fixture
def snapshot_failure_box(tmp_path: Path) -> dict[str, object]:
    return _make_box(
        tmp_path,
        changed_path="infra/compose/Dockerfile.mcp",
        with_mcp_marker=True,
        with_bot_marker=False,
        fail_mktemp=True,
    )


def _run(box: dict[str, object]) -> subprocess.CompletedProcess[str]:
    # Execute the DEPLOY copy, exactly as systemd runs /opt/rebar/infra/scripts/autodeploy.sh.
    return subprocess.run(
        ["bash", str(box["installed"])],
        env=dict(box["env"]),  # type: ignore[arg-type]
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _cmds(box: dict[str, object]) -> list[str]:
    log: Path = box["cmd_log"]  # type: ignore[assignment]
    return log.read_text().splitlines() if log.exists() else []


def _run_lines(cmds: list[str]) -> list[str]:
    return [c for c in cmds if c.startswith("run --name rebar-mcp")]


def _bot_build_lines(cmds: list[str]) -> list[str]:
    return [c for c in cmds if c == "compose-build-review-bot"]


def _marker_file(box: dict[str, object], name: str) -> str:
    p: Path = box["state"]  # type: ignore[assignment]
    f = p / name
    return f.read_text().strip() if f.exists() else ""


# --------------------------------------------------------------------------- #
# AC1 + AC2: the same-tick body change reaches the created container            #
# --------------------------------------------------------------------------- #


def test_body_change_reaches_the_container_built_in_the_same_tick(box: dict[str, object]) -> None:
    """A commit whose ONLY container-constructing change is a new flag in mcp_run_new must
    produce an mcp container carrying that flag — asserted on the constructed `docker run` argv.
    Before the fix the container is built from the STALE parsed body and the flag is absent."""
    result = _run(box)
    cmds = _cmds(box)
    ctx = f"rc={result.returncode}\ncmds={cmds}\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"

    runs = _run_lines(cmds)
    assert runs, f"an mcp container should be created during the tick\n{ctx}"
    assert _BODY_MARKER in runs[0], (
        "the container was built from the STALE mcp_run_new body — the new flag is absent from "
        f"the constructed `docker run` argv\n{ctx}"
    )
    assert result.returncode == 0, f"a healthy mcp cutover must succeed\n{ctx}"
    assert len(runs) == 1, f"exactly one mcp container should be created\n{ctx}"


# --------------------------------------------------------------------------- #
# AC3: no re-exec or redeploy loop                                              #
# --------------------------------------------------------------------------- #


def test_consecutive_ticks_roll_exactly_once_no_loop(box: dict[str, object]) -> None:
    """Two consecutive ticks: the first deploys (a single roll, via one re-exec into the new
    body); the second, with the target unchanged, is a no-op. Across both there is exactly ONE
    mcp roll — the re-exec neither double-rolls within a tick nor re-fires every tick."""
    first = _run(box)
    after_first = list(_cmds(box))
    second = _run(box)
    after_second = list(_cmds(box))
    ctx = (
        f"first_rc={first.returncode} second_rc={second.returncode}\n"
        f"cmds_after_first={after_first}\ncmds_after_second={after_second}\n"
        f"first_out={first.stdout}\nsecond_out={second.stdout}"
    )

    assert first.returncode == 0 and second.returncode == 0, ctx
    assert len(_run_lines(after_first)) == 1, f"the first tick must roll mcp exactly once\n{ctx}"
    assert len(_run_lines(after_second)) == 1, (
        f"the second tick against an unchanged target must add NO roll (total stays 1)\n{ctx}"
    )
    assert "up to date" in second.stdout, (
        f"the second tick must reach the up-to-date no-op, not another deploy\n{ctx}"
    )


# --------------------------------------------------------------------------- #
# AC4: the component marker cannot claim a body that was not applied            #
# --------------------------------------------------------------------------- #


def test_marker_state_matches_the_container_that_was_built(box: dict[str, object]) -> None:
    """After the tick the mcp-deployed-sha marker reads the target AND the container it points
    at was built from the target's body (the marker's flag is present). The marker cannot report
    'deployed at TARGET' over a container built from the previous body."""
    result = _run(box)
    cmds = _cmds(box)
    ctx = f"rc={result.returncode}\ncmds={cmds}\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"

    marker = _marker_file(box, "mcp-deployed-sha")
    runs = _run_lines(cmds)
    assert marker == _TARGET, f"the mcp marker should advance to the target\n{ctx}"
    assert runs and _BODY_MARKER in runs[0], (
        "the marker reads TARGET but the created container lacks TARGET's body flag — a marker "
        f"claiming a body that was never applied\n{ctx}"
    )


# --------------------------------------------------------------------------- #
# defect C: a component behind while the global marker is current still rolls    #
# (recovery: resetting only the component marker must take effect)              #
# --------------------------------------------------------------------------- #


def test_component_marker_behind_global_current_still_rolls(box: dict[str, object]) -> None:
    """The global marker already reads the target but the mcp marker was reset to the parent (the
    documented recovery for a mis-stamped component). The tick must recompute the component delta
    and roll mcp, not exit early at 'up to date; no-op'."""
    state: Path = box["state"]  # type: ignore[assignment]
    (state / "deployed-sha").write_text(_TARGET + "\n")  # global current
    (state / "mcp-deployed-sha").write_text(_DEPLOYED + "\n")  # component behind

    result = _run(box)
    cmds = _cmds(box)
    ctx = f"rc={result.returncode}\ncmds={cmds}\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"

    assert result.returncode == 0, ctx
    assert "no-op" not in result.stdout, (
        f"a behind component must NOT be masked by the global up-to-date early exit\n{ctx}"
    )
    runs = _run_lines(cmds)
    assert runs and _BODY_MARKER in runs[0], (
        f"resetting only the component marker must force an mcp roll of the new body\n{ctx}"
    )


# --------------------------------------------------------------------------- #
# bot path: the same self-update guard applies before review-bot rebuild       #
# --------------------------------------------------------------------------- #


def test_bot_path_reexecs_before_review_bot_rebuild(bot_box: dict[str, object]) -> None:
    """A review-bot-only source delta overwrites autodeploy.sh before the bot rebuild. The
    rebuild must run from the refreshed body, not the stale body that started the tick."""
    result = _run(bot_box)
    cmds = _cmds(bot_box)
    ctx = f"rc={result.returncode}\ncmds={cmds}\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"

    assert result.returncode == 0, ctx
    assert _BOT_BODY_MARKER in result.stdout, (
        f"the review-bot rebuild ran from the STALE deploy_review_bot body\n{ctx}"
    )
    assert _bot_build_lines(cmds) == ["compose-build-review-bot"], (
        f"the review-bot should be rebuilt exactly once\n{ctx}"
    )
    assert "generation 1" in result.stdout, f"the bot path should use a single self re-exec\n{ctx}"


def test_bot_consecutive_ticks_rebuild_exactly_once_no_loop(bot_box: dict[str, object]) -> None:
    """Two consecutive bot-only ticks mirror the mcp cap semantics: the first re-execs into
    the new body and rebuilds once; the second target-current tick is a no-op."""
    first = _run(bot_box)
    after_first = list(_cmds(bot_box))
    second = _run(bot_box)
    after_second = list(_cmds(bot_box))
    ctx = (
        f"first_rc={first.returncode} second_rc={second.returncode}\n"
        f"cmds_after_first={after_first}\ncmds_after_second={after_second}\n"
        f"first_out={first.stdout}\nsecond_out={second.stdout}"
    )

    assert first.returncode == 0 and second.returncode == 0, ctx
    assert _bot_build_lines(after_first) == ["compose-build-review-bot"], (
        f"the first tick must rebuild review-bot exactly once\n{ctx}"
    )
    assert _bot_build_lines(after_second) == ["compose-build-review-bot"], (
        f"the second tick against an unchanged target must add NO rebuild (total stays 1)\n{ctx}"
    )
    assert "up to date" in second.stdout, (
        f"the second tick must reach the up-to-date no-op, not another deploy\n{ctx}"
    )


def test_snapshot_failure_fails_closed_loudly(snapshot_failure_box: dict[str, object]) -> None:
    result = _run(snapshot_failure_box)
    ctx = f"rc={result.returncode}\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"

    assert result.returncode == 1, ctx
    assert "AUTODEPLOY_ERROR" in result.stderr, ctx
    assert "self-update-snapshot-failed" in result.stderr, ctx
