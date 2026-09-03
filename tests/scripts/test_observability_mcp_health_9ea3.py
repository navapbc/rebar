"""The MCP serving path is probed and published as a 1/0 heartbeat (bug 9ea3-7d07-ea55-4496).

On 2026-09-02 the mcp container was OOM-killed mid-gate. nginx stayed healthy while its
single-server ``upstream rebar_mcp`` kept pointing at the dead backend, so ``/mcp`` returned
502 for ~12 hours and a HUMAN reported it: no metric watched the serving path at all.
``observability.sh`` §1b now GETs ``https://<domain>/mcp`` — through TLS, nginx, and the
materialized upstream include, exactly what a client hits — and publishes
``rebar/host:mcp_healthy``.

Two properties are load-bearing and are what these tests pin:

* **401 is the healthy code.** ``/mcp`` requires a bearer PAT, so an unauthenticated probe is
  answered 401 by the app's own auth middleware, which nginx cannot synthesise for that
  location. A "2xx == healthy" rule would report a healthy server as DOWN.
* **It is a heartbeat, not an event.** A value is published on EVERY tick including the
  unhealthy and probe-failed paths. ``monitoring_9ea3.tf`` sets
  ``treat_missing_data = "breaching"`` on exactly that ground, so a silent bad path would
  leave the alarm with no datapoint — the fail-open ticket bff5-9163-cddd-4158 removed.

Every assertion is on the ``aws cloudwatch put-metric-data`` argv the script emits — no
network, no AWS, no real ``curl``.
"""

from __future__ import annotations

import shlex
import subprocess
import textwrap
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"
_SHA = "a" * 40
_OFFSET_VARIABLES = (
    "REPL_OFFSET_FILE",
    "VOTER_OFFSET_FILE",
    "MERGE_OFFSET_FILE",
    "DEPLOY_OFFSET_FILE",
    "DEFER_OFFSET_FILE",
    "INTERRUPT_OFFSET_FILE",
    "G2P_OFFSET_FILE",
)


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def _curl_stub(*, mcp_code: str | None) -> str:
    """Answer the ``/mcp`` probe with ``mcp_code``; ``None`` fails the way a dead network does.

    Every other ``curl`` the script makes keeps its ordinary healthy answer, so a failure
    asserted below can only have come from the ``/mcp`` probe.
    """
    if mcp_code is None:
        # curl on a connection failure: prints its own "000" for %{http_code} and exits 7.
        mcp_case = "printf '000'; exit 7"
    else:
        mcp_case = f"printf {shlex.quote(mcp_code)}; exit 0"
    return f"""
        for a in "$@"; do
          case "$a" in
            *projects/rebar/branches/main*)
              printf ")]}}'\\n"; printf '{{"revision": "{_SHA}"}}\\n'; exit 0 ;;
            */mcp) {mcp_case} ;;
          esac
        done
        case "$*" in *http_code*) printf '200'; exit 0 ;; esac
        printf 'dummy-token'; exit 0
        """


def _environment(tmp_path: Path, *, mcp_code: str | None) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws_log = tmp_path / "aws.log"

    _stub(bin_dir, "curl", _curl_stub(mcp_code=mcp_code))
    _stub(bin_dir, "git", f'printf "{_SHA}\\trefs/heads/main\\n"; exit 0')
    _stub(bin_dir, "logger", "exit 0")
    _stub(bin_dir, "journalctl", "exit 0")
    _stub(bin_dir, "aws", 'printf \'%s\\n\' "$*" >> "$AWS_LOG"; exit 0')

    offsets = tmp_path / "offsets"
    offsets.mkdir()
    repl_log = tmp_path / "replication.log"
    repl_log.write_text("")

    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "REPL_LOG": str(repl_log),
            **{name: str(offsets / name.lower()) for name in _OFFSET_VARIABLES},
        }
    )
    return env, aws_log


def _values(log: Path, metric: str) -> list[int]:
    values: list[int] = []
    for line in log.read_text().splitlines():
        parts = line.split()
        if "--metric-name" not in parts or metric not in parts:
            continue
        values.append(int(parts[parts.index("--value") + 1]))
    return values


def _run(env: dict[str, str]) -> None:
    result = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)
    assert result.returncode == 0


def test_the_auth_challenge_is_the_healthy_signal(tmp_path: Path) -> None:
    """A live serving path answers the unauthenticated probe 401, and that publishes 1.

    This is the case a naive "2xx == healthy" rule gets exactly backwards: the healthy
    production response to this request is a 401, so such a rule would page continuously
    against a perfectly healthy server.
    """
    env, aws_log = _environment(tmp_path, mcp_code="401")

    _run(env)

    assert _values(aws_log, "mcp_healthy") == [1]


def test_a_healthy_probe_never_publishes_zero(tmp_path: Path) -> None:
    """ANTI-VACUITY: the tests below are worthless if the probe publishes 0 unconditionally."""
    env, aws_log = _environment(tmp_path, mcp_code="401")

    _run(env)

    published = _values(aws_log, "mcp_healthy")
    assert 0 not in published, (
        f"a healthy serving path published {published} for mcp_healthy; a 0 here would page "
        "the rebar-mcp-serving-path-down alarm against a working server."
    )


@pytest.mark.parametrize(
    ("code", "what_it_means"),
    [
        ("502", "nginx has no live backend behind `upstream rebar_mcp` — the bug 9ea3 outage"),
        ("503", "the upstream is unavailable"),
        ("504", "the upstream accepted nothing within the gateway timeout"),
        ("404", "the /mcp location binding was lost and the request fell through to Gerrit"),
        ("200", "something answered that is NOT the auth-guarded mcp app"),
    ],
)
def test_an_unhealthy_serving_path_publishes_zero_not_nothing(
    tmp_path: Path, code: str, what_it_means: str
) -> None:
    """Silence is the one value this section may not emit on the bad path.

    ``monitoring_9ea3.tf`` treats missing data as breaching, so publishing nothing would
    still eventually alarm — but only via the dead-publisher path, which is indistinguishable
    from a crashed probe and points the responder at the wrong system. 0 is the honest,
    actionable report, and it is what the alarm's 300/3/2 window is sized to latch.
    """
    env, aws_log = _environment(tmp_path, mcp_code=code)

    _run(env)

    assert _values(aws_log, "mcp_healthy") == [0], (
        f"the serving path returned {code} ({what_it_means}) and the probe published "
        f"{_values(aws_log, 'mcp_healthy')} for mcp_healthy; it must publish exactly [0]."
    )


def test_a_failed_probe_publishes_zero_not_nothing(tmp_path: Path) -> None:
    """A curl that cannot complete at all (TLS/DNS/timeout) still emits a datapoint."""
    env, aws_log = _environment(tmp_path, mcp_code=None)

    _run(env)

    assert _values(aws_log, "mcp_healthy") == [0], (
        "the probe itself failed and mcp_healthy was published as "
        f"{_values(aws_log, 'mcp_healthy')}; an unmakeable probe must report 0, not fall "
        "silent — the heartbeat contract monitoring_9ea3.tf's treat_missing_data relies on."
    )


def test_the_sibling_health_probes_are_unaffected(tmp_path: Path) -> None:
    """A dead MCP must not perturb gerrit_healthy/reviewbot_healthy — they are separate services.

    This is the observation the outage inverted: those two read 1 throughout while /mcp was
    502ing, which is correct behaviour and precisely why mcp_healthy had to be its own metric.
    """
    env, aws_log = _environment(tmp_path, mcp_code="502")

    _run(env)

    assert _values(aws_log, "gerrit_healthy") == [1]
    assert _values(aws_log, "reviewbot_healthy") == [1]
    assert _values(aws_log, "mcp_healthy") == [0]


def test_upstream_lost_mid_gate_latches_a_sustained_zero(tmp_path: Path) -> None:
    """AC(c): the loss of the MCP upstream WHILE A GATE JOB IS ACTIVE is covered.

    The 2026-09-02 shape exactly: a plan-review gate was ~3 minutes in when the kernel
    OOM-killed the container, and nginx — whose ``upstream rebar_mcp`` is one server line
    with no failover — kept proxying to the corpse. From the edge that is indistinguishable
    from any other dead backend, which is the point: the probe needs no knowledge of gate
    activity to see it, and an in-flight gate cannot mask it.

    Three consecutive ticks are asserted, not one, because the alarm latches on
    ``datapoints_to_alarm = 2`` of ``evaluation_periods = 3``. A probe that reported the
    outage once and then fell silent (or recovered to 1 because a *later* container exists on
    some other port) would never reach that count, and the 12-hour blind window would reopen.
    """
    env, aws_log = _environment(tmp_path, mcp_code="502")

    _run(env)
    _run(env)
    _run(env)

    assert _values(aws_log, "mcp_healthy") == [0, 0, 0], (
        "a persistently dead upstream must publish a 0 on EVERY tick so the 2-of-3 window in "
        f"monitoring_9ea3.tf can latch; got {_values(aws_log, 'mcp_healthy')}."
    )
