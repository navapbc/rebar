"""Every container is bounded, no bound is below what its service measurably needs, and the
over-subscription that results is PINNED so it cannot grow in silence.

Story ``48f0-f7ff-c8df-43ac`` (``cryptozoic-unlit-ladybug``), epic ``0480-df66-b3df-4aee``.

THE DEFECT. ``infra/compose/docker-compose.yml`` declared no ``mem_limit`` and no
``deploy.resources`` on ANY of its four services, and ``docker stats`` confirmed it live: every
container reported its limit as ``7.631GiB``, the host total, which is docker's way of saying
there is no limit at all. With no per-container ceiling the kernel picks the OOM victim itself,
which is why the 2026-09-05 exhaustion WEDGED the host instead of degrading — nothing named a
container to fail, so the pressure landed wherever the OOM killer chose.

WHAT A LIMIT BUYS HERE, AND WHAT IT DOES NOT. It buys ATTRIBUTION: an mcp overrun kills mcp,
loudly and by name, instead of letting the kernel choose gerrit. It does NOT buy a reservation
— cgroup ``memory.max`` reserves nothing — but after the r7g.large resize the honest limits fit:

    gerrit      3072 MiB   configured reservation (gerrit.config heapLimit 2g + packedGitLimit
                           1g); measured use 639-795 MiB
    mcp         3712 MiB   measured steady 2941 MiB + one plan-review gate 714 MiB
    review-bot  2048 MiB   measured 45-min max 1886 MiB with a gate active
    opcert       256 MiB   measured 41 MiB, flat
    ------------------------------------------------------------------------
    sum         9088 MiB   against a 15678 MiB host ->  6590 MiB headroom

Trimming gerrit below its configured reservation, or redefining the limits as ceilings until
they fit the old t4g.large, would both be the accommodation-instead-of-fix pattern epic 0480
exists to stop. The arithmetic depends on the resize; if the host shrinks back, this guard
fails rather than silently reintroducing over-subscription.

WHY THESE THREE PROPERTIES. Coverage alone is not enough: a limit set BELOW a service's real
demand converts "unbounded" into "restart loop", which is worse than what it replaced. So the
floor test guards under-sizing, the ceiling test guards the over-subscription growing quietly,
and each is seeded with the configuration it must reject.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = _ROOT / "infra" / "compose" / "docker-compose.yml"
_GERRIT_CONFIG = _ROOT / "infra" / "compose" / "gerrit.config"

#: `free -m` total on the r7g.large, measured 2026-09-09 06:20Z. NOT 16384: firmware and the
#: kernel take their cut before userspace sees any of it.
_HOST_MIB = 15678

#: The smallest limit each service may carry without turning a bound into a restart loop, in
#: MiB, each from a measurement recorded in the module docstring. These are FLOORS, not the
#: declared values -- the declared limit may exceed them, and should.
_MEASURED_NEED_MIB = {
    "gerrit": 3072,  # gerrit.config's own reservation, re-derived below rather than trusted
    "mcp": 3655,  # 2941 steady + 714 for one plan-review gate
    "review-bot": 1886,  # observed 45-minute maximum with a gate running
    "opcert": 41,  # observed, flat across 48h
}

#: What the four limits are allowed to add up to. Deliberately equal to the reviewed sum:
#: any increase has to be a deliberate edit here with a reason, not a quiet consequence of
#: raising one service.
_RECORDED_SUM_CEILING_MIB = 9088
_MIN_HOST_HEADROOM_MIB = 1024

_SIZE_RE = re.compile(r"^(\d+)([bkmg]?)$", re.IGNORECASE)
_MULTIPLIER = {"": 1 / 1048576, "b": 1 / 1048576, "k": 1 / 1024, "m": 1.0, "g": 1024.0}


def _mib(value: object) -> float:
    """A compose/git size literal (``3072m``, ``2g``, a bare byte count) in MiB."""
    match = _SIZE_RE.match(str(value).strip())
    assert match, f"unparseable memory literal: {value!r}"
    return int(match.group(1)) * _MULTIPLIER[match.group(2).lower()]


def _services() -> dict[str, dict]:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]


def _limit_mib(service: dict) -> float | None:
    """The service's declared ceiling in MiB, by either compose spelling."""
    if "mem_limit" in service:
        return _mib(service["mem_limit"])
    limits = service.get("deploy", {}).get("resources", {}).get("limits", {})
    return _mib(limits["memory"]) if "memory" in limits else None


def test_the_parser_finds_exactly_the_services_it_guards() -> None:
    """ANTI-VACUITY. Pinned to the set rather than to a floor, because a service REMOVED from
    compose without this noticing is as much a gap as one added unbounded: either way the set
    this guard believes it covers has stopped matching."""
    assert set(_services()) == set(_MEASURED_NEED_MIB)


def test_every_service_declares_a_memory_limit() -> None:
    """An unbounded container is one the kernel may kill INSTEAD of the one that grew."""
    unbounded = sorted(name for name, svc in _services().items() if _limit_mib(svc) is None)
    assert not unbounded, (
        "compose service(s) declare no memory ceiling, so a memory excursion is arbitrated by "
        "the kernel OOM killer rather than by a named container restart: "
        + ", ".join(unbounded)
        + ". Add `mem_limit` sized from measured container_memory_rss_bytes plus gate headroom."
    )


def test_no_limit_is_below_what_its_service_measurably_needs() -> None:
    """A ceiling under real demand is a restart loop, which is WORSE than being unbounded.

    The failure it prevents is concrete: mcp sits at 2941 MiB before any gate runs, so a limit
    picked to make the four sum under 7813 MiB would have to be below that and would kill the
    server on startup, every time.
    """
    under = [
        f"{name} limit={_limit_mib(svc)} MiB < measured need {_MEASURED_NEED_MIB[name]} MiB"
        for name, svc in _services().items()
        if (_limit_mib(svc) or 0) < _MEASURED_NEED_MIB[name]
    ]
    assert not under, (
        "compose memory limit(s) are below the service's measured demand, so the container "
        "would be OOM-killed for doing its ordinary work: " + "; ".join(under) + ". Raise the "
        "limit; do NOT lower the recorded need to match."
    )


def test_gerrit_is_not_capped_below_its_own_configured_reservation() -> None:
    """Read from gerrit.config so the compose limit and the JVM's own ceiling cannot drift.

    This is what stops the tempting fix: gerrit uses only 639-795 MiB against a 3 GiB
    reservation, so trimming it is the obvious way to make the sum close. It is refused here
    because the reservation is what gerrit is CONFIGURED to be allowed to take, and a limit
    below it OOM-kills the JVM for growing into memory it was told it could use.
    """
    config = _GERRIT_CONFIG.read_text(encoding="utf-8")
    reserved = 0.0
    for key in ("heapLimit", "packedGitLimit"):
        match = re.search(rf"^\s*{key}\s*=\s*(\S+)\s*$", config, re.MULTILINE)
        assert match, f"{key} not found in gerrit.config — this guard has stopped matching"
        reserved += _mib(match.group(1))
    gerrit = _limit_mib(_services()["gerrit"])
    assert gerrit is not None and gerrit >= reserved, (
        f"gerrit's compose mem_limit ({gerrit} MiB) is below the {reserved:.0f} MiB "
        f"gerrit.config already reserves (heapLimit + packedGitLimit)."
    )


def test_the_memory_limits_fit_the_resized_host_with_headroom() -> None:
    """The sum is now below host RAM; raising any limit must preserve that property."""
    limits = {name: _limit_mib(svc) for name, svc in _services().items()}
    total = sum(value for value in limits.values() if value is not None)
    assert total <= _RECORDED_SUM_CEILING_MIB, (
        f"compose memory limits now sum to {total:.0f} MiB, above the recorded "
        f"{_RECORDED_SUM_CEILING_MIB} MiB. The host has {_HOST_MIB} MiB, so this figure is "
        f"the reviewed sum. Growing it needs an explicit decision here, not a silent bump. "
        f"Per-service: {limits}."
    )
    assert _HOST_MIB - total >= _MIN_HOST_HEADROOM_MIB, (
        f"compose memory limits sum to {total:.0f} MiB on a {_HOST_MIB} MiB host, leaving "
        f"{_HOST_MIB - total:.0f} MiB; story 48f0-f7ff-c8df-43ac requires at least "
        f"{_MIN_HOST_HEADROOM_MIB} MiB for OS/page cache headroom."
    )
