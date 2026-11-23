"""Every long-running rebar container runs an init as PID 1, so orphaned processes are reaped.

Bug ``585e-8f49-dff0-4ada`` (ultrashort-emaciated-krill). With no ``init`` the server process
(uvicorn, ``rebar-mcp``) was the container's PID 1. Git 2.47 and later start
``git maintenance run --auto --detach`` after a commit, fetch or merge; that child forks, its
parent exits, and the daemonized ``git`` is re-parented to PID 1. A Python server never waits
for children it did not start, so each one stayed a ``[git] <defunct>`` until the container was
recreated: 17 on the review-bot, and 3 plus the entrypoint's provisioning subshell on the MCP
container. Docker's bundled ``docker-init`` (tini) as PID 1 reaps them whatever started them.

The containers are started two ways, and both are guarded: compose services
(``init: true``) and the blue-green MCP ``docker run`` in autodeploy (``--init``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = _ROOT / "infra" / "compose" / "docker-compose.yml"
_SCRIPTS = _ROOT / "infra" / "scripts"
_ENTRYPOINT = _SCRIPTS / "mcp-entrypoint.sh"

#: The services compose starts. Pinned so a service added without this guard noticing fails.
_SERVICES = {"gerrit", "review-bot", "opcert", "mcp"}


def _services() -> dict[str, dict]:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]


def _services_without_init(services: dict[str, dict]) -> list[str]:
    return sorted(name for name, svc in services.items() if svc.get("init") is not True)


def _detached_runs(script: str) -> list[str]:
    """Each ``docker run`` command in *script* that starts a detached container, with its
    backslash-continued argument lines joined."""
    joined = re.sub(r"\\\n", " ", script)
    return [
        line.strip()
        for line in joined.splitlines()
        if re.search(r"\bdocker run\b", line) and re.search(r"\s(-d|--detach)\b", line)
    ]


def _runs_without_init(script: str) -> list[str]:
    return [run for run in _detached_runs(script) if not re.search(r"\s--init\b", run)]


def test_the_guard_covers_exactly_the_compose_services() -> None:
    """ANTI-VACUITY: a renamed or added service must be noticed, not silently skipped."""
    assert set(_services()) == _SERVICES


def test_every_compose_service_runs_an_init() -> None:
    missing = _services_without_init(_services())
    assert not missing, (
        "compose service(s) run their own process as PID 1, which never reaps orphaned "
        f"git children: {', '.join(missing)}. Set `init: true`."
    )


def test_the_service_check_rejects_a_service_without_init() -> None:
    seeded = {"ok": {"init": True}, "bare": {}, "off": {"init": False}}
    assert _services_without_init(seeded) == ["bare", "off"]


def test_every_detached_docker_run_in_the_host_scripts_runs_an_init() -> None:
    runs: dict[str, list[str]] = {}
    for path in sorted(_SCRIPTS.glob("*.sh")):
        found = _detached_runs(path.read_text(encoding="utf-8"))
        if found:
            runs[path.name] = found
    assert "autodeploy.sh" in runs, "the blue-green MCP `docker run -d` was not found"
    missing = {
        name: bad
        for name, found in runs.items()
        if (bad := [r for r in found if not re.search(r"\s--init\b", r)])
    }
    assert not missing, f"detached `docker run` without `--init`: {missing}"


def test_the_run_check_rejects_a_detached_run_without_init() -> None:
    seeded = (
        'docker run -d --name "$1" \\\n    --restart always \\\n    img\n'
        "docker run -d --init --name x img\n"
        "docker run --rm --entrypoint sh img -c true\n"
    )
    assert len(_detached_runs(seeded)) == 2
    assert _runs_without_init(seeded) == [
        'docker run -d --name "$1"      --restart always      img'
    ]


def test_the_mcp_entrypoint_detaches_provisioning_so_the_init_reaps_it() -> None:
    """`provision_store &` before `exec` left the subshell a child of the exec'd server, which
    never waits for it. The `( ... & )` form re-parents it to the init."""
    body = _ENTRYPOINT.read_text(encoding="utf-8")
    assert re.search(r"^\(\s*provision_store\s*&\s*\)\s*$", body, re.MULTILINE), (
        "start provisioning as `( provision_store & )` so it is re-parented to the init"
    )
    assert not re.search(r"^provision_store\s*&\s*$", body, re.MULTILINE)
