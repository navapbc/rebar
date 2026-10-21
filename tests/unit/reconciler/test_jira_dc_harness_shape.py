"""Per-commit structural enforcement for the Jira DC harness (story J5, epic e369).

The harness itself only runs on a native-amd64 runner from a weekly/dispatched
workflow. Its *file shape*, though, encodes several decisions that are expensive
to rediscover and easy to "tidy" away — so those are pinned HERE, in the default
unit tier that CI runs on every commit, rather than under ``tests/external/``
where the opt-in guard would leave them unchecked except on a cron.

The two that matter most, both learned by watching the harness fail:

* **``-Dproduct.start.timeout``** — without it, Cargo's default 600000 ms deploy
  ceiling kills the container roughly 25 minutes in with
  ``Deployable [...] failed to finish deploying within the timeout period
  [600000]``. That reads like a network fault, so a maintainer is likely to
  delete the "unnecessary" flag and reintroduce a slow, silent death.
  (``-DstartupTimeout`` and ``-Dcargo.timeout`` were both verified to have NO
  effect — this is the only property that works.)
* **``-DskipAllPrompts=true``** — Atlassian retired the Marketplace v1 REST
  endpoint that AMPS hardcodes for its SDK update check, so without this flag
  every cold start dies on a ``FileNotFoundException``.

Plus the security and reproducibility invariants: the base image is pinned by
DIGEST (not a floating tag), and the port is bound to loopback only, because the
instance ships well-known ``admin``/``admin`` credentials.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest
from _tree_scan import parsed_python_files

_REPO = Path(__file__).resolve().parents[3]
_HARNESS = _REPO / "tests" / "external" / "live_jira_dc"
_DOCKERFILE = _HARNESS / "Dockerfile"
_COMPOSE = _HARNESS / "docker-compose.yml"
_PYCONTRIBS_LICENSE = _HARNESS / "pycontribs-jira-LICENSE.txt"
_PYCONTRIBS_LICENSE_SHA256 = "a432e25d7fa27b288cc7ab6ea8e3a9aa27c101579de7fad7a3b5f534ba9e772a"


def test_vendored_pycontribs_license_matches_the_pinned_source() -> None:
    """The stored license bytes match the selected upstream revision."""
    assert _PYCONTRIBS_LICENSE.is_file(), f"missing {_PYCONTRIBS_LICENSE}"
    digest = hashlib.sha256(_PYCONTRIBS_LICENSE.read_bytes()).hexdigest()
    assert digest == _PYCONTRIBS_LICENSE_SHA256


def test_harness_directory_exists_under_the_external_tier() -> None:
    """Precondition for everything below — and itself an assertion: the harness
    lives under ``tests/external/``, the only tier fenced off from per-commit CI."""
    assert _HARNESS.is_dir(), f"{_HARNESS} missing — the harness must live under tests/external/"
    assert _DOCKERFILE.is_file()
    assert _COMPOSE.is_file()


# ---------------------------------------------------------------------------
# The Dockerfile: pinned base + the two mandatory AMPS flags
# ---------------------------------------------------------------------------


def test_base_image_is_pinned_by_digest_not_a_floating_tag() -> None:
    """A tag would let the upstream shift under us; the digest makes the harness
    reproducible. Mirrors what pycontribs/jira's own wrapper does."""
    text = _DOCKERFILE.read_text()
    from_lines = [ln for ln in text.splitlines() if ln.strip().upper().startswith("FROM ")]

    assert from_lines, "Dockerfile has no FROM line"
    for line in from_lines:
        assert "@sha256:" in line, (
            f"base image must be pinned by digest, got: {line.strip()!r}. A floating "
            f"tag makes the harness non-reproducible."
        )


@pytest.mark.parametrize(
    ("flag", "why"),
    [
        (
            "-DskipAllPrompts=true",
            "Atlassian retired the Marketplace v1 endpoint AMPS hardcodes for its SDK "
            "update check; without this flag EVERY cold start dies on FileNotFoundException",
        ),
        (
            "-Dproduct.start.timeout",
            "without it Cargo's default 600000ms deploy ceiling kills the container ~25 "
            "minutes in; -DstartupTimeout and -Dcargo.timeout were verified to have NO effect",
        ),
    ],
)
def test_entrypoint_keeps_the_mandatory_amps_flag(flag: str, why: str) -> None:
    """Both flags look like removable noise and both are load-bearing.

    Asserted against the ENTRYPOINT/CMD INSTRUCTION, not the whole file. A
    file-wide substring check is hollow here, and provably so: the sibling test
    below REQUIRES these flags to be explained in comments, so once that comment
    exists a ``flag in text`` assertion passes even after the flag is deleted from
    the entrypoint. Mutation-tested — removing the flag from the instruction must
    fail this.
    """
    instructions = "\n".join(
        line
        for line in _DOCKERFILE.read_text().splitlines()
        if not line.lstrip().startswith("#")
        and line.strip().upper().startswith(("ENTRYPOINT", "CMD"))
    )
    assert instructions, "Dockerfile declares no ENTRYPOINT/CMD"
    assert flag in instructions, (
        f"the ENTRYPOINT lost {flag} — {why}. (Present in a comment is not enough; "
        f"it must be on the actual instruction.)"
    )


def test_dockerfile_records_why_the_timeout_flag_exists() -> None:
    """The flag alone is not enough: a reader needs the failure it prevents, or the
    next person deletes it. Require the ceiling value to appear in a comment."""
    comments = "\n".join(
        ln for ln in _DOCKERFILE.read_text().splitlines() if ln.lstrip().startswith("#")
    )
    assert "600000" in comments, (
        "the Dockerfile must record, in a comment, that Cargo's default 600000ms deploy "
        "ceiling is what -Dproduct.start.timeout overrides — otherwise the flag reads as "
        "noise and gets deleted"
    )


def test_entrypoint_does_not_use_the_verified_ineffective_flags() -> None:
    """The two plausible-looking property names that DO NOT work.

    ``-DstartupTimeout`` and ``-Dcargo.timeout`` are the obvious guesses and both
    were measured to have NO effect — the container still died at the same
    ``timeout period [600000]``. Their appearance on the actual instruction would
    mean someone "fixed" the timeout with a known-dead flag, reintroducing the
    silent death while looking like they had addressed it. A cautionary COMMENT
    naming them is fine and expected, which is why this checks the instruction.
    """
    instructions = [
        line
        for line in _DOCKERFILE.read_text().splitlines()
        if not line.lstrip().startswith("#")
        and line.strip().upper().startswith(("ENTRYPOINT", "CMD"))
    ]
    joined = "\n".join(instructions)

    for dud in ("-DstartupTimeout", "-Dcargo.timeout"):
        assert dud not in joined, (
            f"{dud} is on the ENTRYPOINT but was verified to have NO effect; the "
            f"working property is -Dproduct.start.timeout"
        )


def test_dockerfile_does_not_reference_the_unpublished_image_as_a_source() -> None:
    """``pycontribs/jira-test-image`` is not published anywhere — it is upstream's
    LOCAL build name. It must never appear as a ``FROM``/pull source; only as
    attribution prose."""
    for line in _DOCKERFILE.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        assert "pycontribs/jira-test-image" not in line, (
            f"{line.strip()!r} treats an unpublished image as a source — it does not "
            f"exist in any registry; build from the vendored Dockerfile instead"
        )


# ---------------------------------------------------------------------------
# The compose file: loopback binding, explicit platform, TTY
# ---------------------------------------------------------------------------


def _compose_services() -> dict[str, Any]:
    """Parse the compose file and return its services.

    Parsed, NOT grepped. A raw-text search reads COMMENTS as configuration, and a
    well-commented compose file quotes the very anti-patterns it guards against —
    this file's ports comment names the bare ``"2990:2990"`` form precisely to warn
    against it, which made an earlier substring version of this test fail on
    correct config. Structured files get parsed.
    """
    import yaml

    data = yaml.safe_load(_COMPOSE.read_text())
    services = (data or {}).get("services") or {}
    assert services, f"{_COMPOSE.name} declares no services"
    return services


def test_port_is_bound_to_loopback_only() -> None:
    """This instance has well-known admin/admin credentials. A bare ``2990:2990``
    binds 0.0.0.0 and exposes it to every host on the network."""
    mappings = [
        str(entry)
        for service in _compose_services().values()
        for entry in (service.get("ports") or [])
    ]
    jira_mappings = [m for m in mappings if m.endswith(":2990") or m.endswith("2990")]

    assert jira_mappings, f"no 2990 port mapping found in {_COMPOSE.name}"
    for mapping in jira_mappings:
        assert mapping.startswith("127.0.0.1:"), (
            f"port mapping {mapping!r} must be loopback-scoped (127.0.0.1:2990:2990) — "
            f"this Jira ships admin/admin and must not be network-reachable"
        )


def test_compose_pins_the_amd64_platform_explicitly() -> None:
    """Behaviour must not depend on the host's default platform resolution: the base
    is amd64, and an arm64 host emulating it silently cannot finish booting."""
    platforms = [s.get("platform") for s in _compose_services().values()]

    assert "linux/amd64" in platforms, (
        f"a service must set platform: linux/amd64 explicitly; got {platforms!r}"
    )


def test_compose_allocates_a_tty() -> None:
    """AMPS exits without a controlling TTY, so ``docker run -dit``'s equivalent
    (``tty`` + ``stdin_open``) is required, not optional. Asserted on the PARSED
    booleans — a commented-out ``tty: true`` would satisfy a substring check."""
    services = _compose_services()

    assert any(s.get("tty") is True for s in services.values()), (
        "AMPS exits without a controlling TTY — a service must set tty: true"
    )
    assert any(s.get("stdin_open") is True for s in services.values()), (
        "a service must set stdin_open: true (the -i half of -dit)"
    )


def test_compose_builds_locally_rather_than_referencing_a_pullable_image() -> None:
    """The service must ``build:`` the vendored Dockerfile. An ``image:`` referencing
    ``pycontribs/jira-test-image`` would fail at pull time — that tag exists in no
    registry."""
    services = _compose_services()

    assert any(s.get("build") for s in services.values()), (
        "a service must declare `build:` — the harness image is built locally, never pulled"
    )
    for name, service in services.items():
        image = str(service.get("image") or "")
        assert "pycontribs/jira-test-image" not in image, (
            f"service {name!r} references the unpublished {image!r}; build locally instead"
        )


def test_compose_mounts_a_persistent_maven_cache() -> None:
    """``atlas-run`` fetches Jira (~917 artifacts) from maven.atlassian.com on first
    start. Without a persistent ``/root/.m2`` mount every fresh container repeats
    that download, which dominates startup."""
    mounts = [
        str(v) for service in _compose_services().values() for v in (service.get("volumes") or [])
    ]

    assert any(".m2" in m for m in mounts), (
        f"expected a persistent Maven cache mounted at /root/.m2; got {mounts!r}"
    )


# ---------------------------------------------------------------------------
# Tier hygiene: nothing here may carry the `integration` marker
# ---------------------------------------------------------------------------


def test_no_harness_module_carries_the_integration_marker() -> None:
    """The marker that would drag this into per-commit CI.

    ``integration`` is NOT an exclusion: ``_build-and-test.yml`` runs
    ``pytest -m integration`` as its own step on every commit, explicitly for tests
    that "need no live services". A Docker-requiring test marked that way would run
    there against a runner with no container, no Jira and no egress.

    Asserting the ABSENCE of ``integration`` is the assertion with teeth. Asserting
    the PRESENCE of ``external`` would be vacuous — ``tests/external/conftest.py``
    auto-applies it to everything under that tree, so such a test could never fail.
    """
    offenders = []
    for module in parsed_python_files(_HARNESS):
        text = module.source
        if re.search(r"mark\.integration|pytestmark\s*=.*integration", text):
            offenders.append(module.path.name)
    assert not offenders, (
        f"these harness modules carry the `integration` marker and would run in "
        f"per-commit CI without Docker: {offenders}"
    )
