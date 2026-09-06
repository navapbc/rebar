"""Every cap script the probe EXECUTES must exist where a deployed probe looks for it.

``observability.sh`` reads each disk generator's budget by RUNNING a cap script, and resolves
each one relative to its own path. The probe is installed as
``/usr/local/bin/rebar-observability.sh``, so a bare sibling name resolves into
``/usr/local/bin`` — and for two of the four scripts that is a name nothing ever writes.

That gap was not theoretical. On the Gerrit host both invocations failed with rc 127, which
produced two DIFFERENT failures from one cause (bug ``5fb0-89ab-4466-41cc``):

* the gated percent metrics — ``var_tmp_used_percent``, ``container_writable_used_percent`` —
  published NOTHING, because their budget guard reads the value from ``--print-env``; and
* the ungated heartbeats — ``var_tmp_cleanup_active``, ``container_reaper_active`` — published a
  confident, FALSE ``0``, because ``--check-active`` returned the empty string and the ``case``
  coerced it. Both alarms then read 0 on a host whose reapers were genuinely running.

The end-to-end proofs live beside the metrics they belong to, in
``test_observability_var_tmp.py`` and ``test_observability_container_layers.py``. What this file
adds is the STRUCTURAL half those cannot give: a per-script check that the coupling between the
probe's lookup and whatever creates the file still holds, so renaming an installed path or
dropping a script from the installer fails the build instead of silently taking a metric off the
air. A test that runs the probe can only cover the scripts someone remembered to write a case
for; this one is derived from the script's own text and so covers every cap script that exists.

Two resolution strategies are legitimate, and which one applies is a property of the cap script:

* **Installed by the probe's installer.** ``docker-storage-cap.sh`` and ``journald-cap.sh``
  define no installed path of their own, so their repo basename is the only name they have.
  ``install-observability.sh`` deploys them next to the probe.
* **Resolved at the script's OWN installed name.** ``vartmp-cap.sh`` and ``container-cap.sh``
  self-install as ``rebar-*-cap.sh``, because that path is the ``ExecStart`` of the reaper unit
  each renders. Copying them under the sibling name as well would put a SECOND copy on the box,
  with the probe reading one and the reaper timer executing the other, free to drift. For these
  the probe carries the installed name as a fallback candidate instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "infra" / "scripts"
PROBE = SCRIPTS / "observability.sh"
INSTALLER = SCRIPTS / "install-observability.sh"

# Cap scripts that self-install under a different name, and the variable each declares it in.
SELF_INSTALLING = {
    "vartmp-cap.sh": "VAR_TMP_INSTALLED_PATH",
    "container-cap.sh": "CONTAINER_INSTALLED_PATH",
}

_CAP_SH_LINE = re.compile(r"^(?P<var>[A-Z_]+_CAP_SH)=", re.MULTILINE)
_SIBLING = re.compile(r"&& pwd\)/(?P<name>[a-z0-9-]+\.sh)")
_INSTALLED_CAPS = re.compile(r"^for cap in (?P<names>[^;]+); do", re.MULTILINE)


def _cap_sh_assignments() -> dict[str, str]:
    """``{basename: the full assignment line}`` for every ``*_CAP_SH`` default in the probe."""
    text = PROBE.read_text()
    found: dict[str, str] = {}
    for match in _CAP_SH_LINE.finditer(text):
        line = text[match.start() : text.index("\n", match.start())]
        sibling = _SIBLING.search(line)
        assert sibling, f"{match.group('var')} resolves no sibling name: {line}"
        found[sibling.group("name")] = line
    return found


def _installer_deploys() -> set[str]:
    """The cap basenames ``install-observability.sh`` actually deploys.

    Parsed from the install loop rather than searched for as free text: the surrounding comment
    NAMES the two scripts that are deliberately excluded, so a substring search over the file
    would report them as installed and invert this test's meaning.
    """
    match = _INSTALLED_CAPS.search(INSTALLER.read_text())
    assert match, "install-observability.sh has no `for cap in ...` deploy loop"
    return set(match.group("names").split())


def _declared_installed_path(cap_name: str, variable: str) -> str:
    """The default the cap script itself declares for its installed copy."""
    pattern = re.compile(rf'^{variable}="\$\{{{variable}:-(?P<path>[^}}"]+)\}}"', re.MULTILINE)
    match = pattern.search((SCRIPTS / cap_name).read_text())
    assert match, f"{cap_name} declares no {variable} default"
    return match.group("path")


def test_the_probe_resolves_at_least_the_four_known_cap_scripts() -> None:
    """Guards the discovery itself. If the regex stops matching, every assertion below would
    pass vacuously over an empty set — the shape of failure this whole file exists to stop."""
    assert set(_cap_sh_assignments()) >= {
        "docker-storage-cap.sh",
        "journald-cap.sh",
        "vartmp-cap.sh",
        "container-cap.sh",
    }


@pytest.mark.parametrize("cap_name", sorted(_cap_sh_assignments()))
def test_every_cap_script_the_probe_runs_is_reachable_once_deployed(cap_name: str) -> None:
    """One of the two legitimate strategies must cover each script — and exactly the one that
    matches how that script installs itself."""
    line = _cap_sh_assignments()[cap_name]

    if cap_name in SELF_INSTALLING:
        variable = SELF_INSTALLING[cap_name]
        declared = _declared_installed_path(cap_name, variable)
        assert declared in line, (
            f"{cap_name} self-installs as {declared}, but the probe's candidate list does not "
            f"name it, so a deployed probe cannot reach it: {line}"
        )
        assert cap_name not in _installer_deploys(), (
            f"{cap_name} self-installs as {declared}; installing it under its repo basename too "
            "would leave two copies on the box for the probe and the reaper to disagree over"
        )
    else:
        assert cap_name in _installer_deploys(), (
            f"{cap_name} has no installed path of its own, so install-observability.sh must "
            "deploy it beside the probe; otherwise the probe resolves a path nothing creates"
        )


@pytest.mark.parametrize(("cap_name", "variable"), sorted(SELF_INSTALLING.items()))
def test_the_probe_honours_the_installed_path_override(cap_name: str, variable: str) -> None:
    """The fallback must read through the SAME variable the cap script honours, so an operator
    or test that relocates the installed copy moves the probe's lookup with it rather than
    leaving the two pointing at different files."""
    line = _cap_sh_assignments()[cap_name]
    assert f"${{{variable}:-" in line, (
        f"the probe hard-codes {cap_name}'s installed path instead of reading {variable}, so "
        f"relocating the installed copy silently strands the probe: {line}"
    )


@pytest.mark.parametrize("cap_name", sorted(_cap_sh_assignments()))
def test_the_sibling_candidate_is_tried_first(cap_name: str) -> None:
    """The checkout layout must keep winning wherever it already did. Every other suite runs the
    probe in place from ``infra/scripts``; if an installed path outranked the sibling, a
    developer machine that happens to have a real cap script in ``/usr/local/bin`` would steer
    those suites at the host's copy instead of the tree under test."""
    line = _cap_sh_assignments()[cap_name]
    sibling_at = line.index(cap_name)
    for variable in SELF_INSTALLING.values():
        if variable in line:
            assert sibling_at < line.index(variable), (
                f"{cap_name}'s installed-path candidate precedes the sibling, so a checkout run "
                "can resolve the host's copy instead of the tree under test"
            )


# --------------------------------------------------------------------------------------
# The UNKNOWN sentinel must keep paging (bug 5fb0-89ab-4466-41cc)
# --------------------------------------------------------------------------------------

TERRAFORM = REPO_ROOT / "infra" / "terraform"

# Heartbeats whose value observability.sh sets through `heartbeat_value`, and which an alarm
# consumes. The quota FACTS (`var_tmp_hard_quota_in_effect`, `container_quota_enforceable`) go
# through the same helper but are deliberately unalarmed, so they are out of scope here.
ALARMED_HEARTBEATS = (
    "journal_cap_in_effect",
    "var_tmp_cleanup_active",
    "container_reaper_active",
)

_ALARM_BLOCK = re.compile(
    r'resource\s+"aws_cloudwatch_metric_alarm"\s+"(?P<name>[^"]+)"\s*\{(?P<body>.*?)\n\}',
    re.DOTALL,
)


def _sentinel() -> int:
    match = re.search(r"^HEARTBEAT_UNKNOWN=(-?\d+)$", PROBE.read_text(), re.MULTILINE)
    assert match, "observability.sh declares no HEARTBEAT_UNKNOWN sentinel"
    return int(match.group(1))


def _alarms_for(metric: str) -> list[tuple[str, str]]:
    found = []
    for tf in sorted(TERRAFORM.glob("*.tf")):
        for block in _ALARM_BLOCK.finditer(tf.read_text()):
            body = block.group("body")
            if re.search(rf'^\s*metric_name\s*=\s*"{re.escape(metric)}"\s*$', body, re.MULTILINE):
                found.append((block.group("name"), body))
    return found


@pytest.mark.parametrize("metric", ALARMED_HEARTBEATS)
def test_the_unknown_sentinel_still_breaches_every_alarm_on_that_heartbeat(metric: str) -> None:
    """`-1` must page exactly as `0` does.

    The sentinel's whole justification is that it changes what an operator READS without
    changing who gets woken — which is only true while every alarm on these metrics is
    `LessThanThreshold` with a threshold above the sentinel. Flipping a comparison operator, or
    dropping a threshold to -1 or below, would silently convert "could not determine" into a
    non-event: the alarm would sit OK while nothing on the box was measuring the cap at all.
    That is the exact failure this ticket exists to remove, so it is asserted against the real
    Terraform rather than left as a claim in a comment.
    """
    sentinel = _sentinel()
    alarms = _alarms_for(metric)
    assert alarms, f"no alarm found for {metric}; the sentinel's paging guarantee is unproven"

    for name, body in alarms:
        operator = re.search(r'^\s*comparison_operator\s*=\s*"([^"]+)"', body, re.MULTILINE)
        threshold = re.search(r"^\s*threshold\s*=\s*(-?[\d.]+)", body, re.MULTILINE)
        assert operator and threshold, f"{name} declares no comparison_operator/threshold"
        assert operator.group(1) == "LessThanThreshold", (
            f"{name} watches {metric} with {operator.group(1)}; the {sentinel} sentinel is only "
            "guaranteed to breach under LessThanThreshold"
        )
        assert sentinel < float(threshold.group(1)), (
            f"{name} has threshold {threshold.group(1)}, which the {sentinel} sentinel does not "
            "breach — an unmeasured cap would read as healthy"
        )
