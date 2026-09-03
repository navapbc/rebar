"""Host memory and per-container RSS are measured and published (bug 9ea3-7d07-ea55-4496).

The mcp container was OOM-killed mid-gate on an 8 GiB box that publishes disk and health
metrics and not one byte of memory. §2c adds the measurement. These tests pin the three
properties that make it usable:

* the host gauges publish a MEASURED value on the happy path;
* they publish on the FAILURE path too — a probe that falls silent when ``free`` is
  missing is indistinguishable from a dead host (the fail-open ticket
  bff5-9163-cddd-4158 removed), so the pessimistic value is published and
  ``mem_probe_ok=0`` marks it as synthesised rather than measured;
* the per-container census publishes one datapoint per container dimensioned on a STABLE
  SERVICE identity read from a container label — never the container name, which for the
  blue-green mcp containers embeds the deploy SHA and port and so would mint a fresh
  CloudWatch dimension value on every deploy;
* it publishes its own ``container_stats_ok`` heartbeat when ``docker stats`` fails, times
  out, or ``timeout`` itself is absent, and counts any row it could not parse instead of
  dropping it behind a flag that says the census went fine.

Every assertion is on the ``aws cloudwatch put-metric-data`` argv the real script emits —
no network, no AWS, no real ``docker``.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"

_OFFSET_VARIABLES = (
    "REPL_OFFSET_FILE",
    "VOTER_OFFSET_FILE",
    "MERGE_OFFSET_FILE",
    "DEPLOY_OFFSET_FILE",
    "DEFER_OFFSET_FILE",
    "INTERRUPT_OFFSET_FILE",
    "G2P_OFFSET_FILE",
)

# `free -k`: total used free shared buff/cache available. 8 GiB total, 2 GiB available
# => 25% available / 75% used, which is the arithmetic the script's awk must reproduce.
_FREE_8GIB_2AVAIL = textwrap.dedent(
    """\
                   total        used        free      shared  buff/cache   available
    Mem:         8388608     5242880      524288        1024     2621440     2097152
    Swap:              0           0           0
    """
)


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def _environment(
    tmp_path: Path,
    *,
    free_body: str | None = None,
    docker_body: str | None = None,
    with_timeout: bool = True,
) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws_log = tmp_path / "aws.log"

    _stub(bin_dir, "curl", "printf '200'; exit 0")
    _stub(bin_dir, "git", "exit 128")
    _stub(bin_dir, "logger", "exit 0")
    _stub(bin_dir, "journalctl", "exit 0")
    _stub(bin_dir, "df", "exit 1")
    _stub(bin_dir, "aws", 'printf \'%s\\n\' "$*" >> "$AWS_LOG"; exit 0')

    if free_body is None:
        # `free` absent entirely: the probe must still publish, pessimistically.
        free_body = "exit 127"
    _stub(bin_dir, "free", free_body)

    if docker_body is None:
        docker_body = "exit 1"
    _stub(bin_dir, "docker", docker_body)

    if with_timeout:
        # A faithful stand-in for coreutils `timeout N cmd …`: drop the duration, exec
        # the rest. The script must work through it, not around it.
        _stub(bin_dir, "timeout", 'shift; exec "$@"')
    else:
        # `timeout` absent from the host: exit 127 is what the shell reports for a
        # command it cannot find, so the bounded call fails rather than running unbounded.
        _stub(bin_dir, "timeout", "exit 127")

    offsets = tmp_path / "offsets"
    offsets.mkdir()

    env = subprocess_env()
    # PREPENDED so the stubs shadow any ambient `free`/`docker`/`timeout` — the script
    # itself still needs the real interpreter and coreutils on PATH. Every command the
    # memory section touches has a stub here, so no ambient binary can answer for it and
    # make a failure-path test vacuously pass.
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "REPL_LOG": str(tmp_path / "absent-replication.log"),
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
        values.append(int(float(parts[parts.index("--value") + 1])))
    return values


def _dimensioned(log: Path, metric: str) -> dict[str, list[int]]:
    """``{service: [value, …]}`` for every datapoint of ``metric`` carrying a ``service`` dim.

    A LIST, not a scalar: two containers of the same service are live at once during a
    blue-green cutover and both legitimately publish to the same dimension in one tick, so
    collapsing them here would hide exactly the case the dimension has to survive."""
    out: dict[str, list[int]] = {}
    for line in log.read_text().splitlines():
        parts = line.split()
        if "--metric-name" not in parts or metric not in parts:
            continue
        dims = parts[parts.index("--dimensions") + 1]
        name = next((d.split("=", 1)[1] for d in dims.split(",") if d.startswith("service=")), None)
        if name is not None:
            out.setdefault(name, []).append(int(float(parts[parts.index("--value") + 1])))
    return out


def _run(env: dict[str, str]) -> None:
    result = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)
    assert result.returncode == 0


def _docker(rows: list[dict[str, str]], *, prelude: str = "") -> str:
    """A ``docker`` stub answering BOTH calls the census makes, honouring ``--format``.

    ``docker stats --format`` cannot emit labels, so the script joins a second bounded
    ``docker ps`` to learn each container's service identity. A stub that answered only one
    of the two would let that join pass vacuously, so this one serves both — and the ``ps``
    branch FAILS unless the script actually asked for both label templates, which is the
    only thing that makes "the dimension came from a label" a real assertion rather than a
    restatement of the stub.

    Each row is ``{name, mem, rebar?, compose?}``; an omitted label is the empty string
    docker prints for a label the container does not carry.
    """
    ps_lines = "\n".join(
        f"PS|{r['name']}|{r.get('rebar', '')}|{r.get('compose', '')}" for r in rows
    )
    stats_lines = "\n".join(f"ST|{r['name']}|{r['mem']}" for r in rows if r.get("mem"))
    return (
        f"{prelude}\n"
        'case "$1" in\n'
        "  ps)\n"
        '    case "$*" in\n'
        "      *rebar.service*com.docker.compose.service*) ;;\n"
        "      *) printf 'ps stub: format asked for no service labels: %s\\n' \"$*\" >&2\n"
        "         exit 3 ;;\n"
        "    esac\n"
        f"    cat <<'PSEOF'\n{ps_lines}\nPSEOF\n"
        "    ;;\n"
        "  stats)\n"
        f"    cat <<'STEOF'\n{stats_lines}\nSTEOF\n"
        "    ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )


# The four services on the box, as `docker ps` reports them. Only the blue-green mcp
# container carries `rebar.service`: it is started by a bare `docker run` in autodeploy.sh
# that compose never sees. Everything else is compose-managed and already labelled.
_BOX_ROWS = [
    {"name": "compose-gerrit-1", "mem": "3.5GiB / 7.664GiB", "compose": "gerrit"},
    {"name": "rebar-mcp-3e04025b684e-8092", "mem": "512MiB / 7.664GiB", "rebar": "mcp"},
    {"name": "compose-review-bot-1", "mem": "128.5MiB / 7.664GiB", "compose": "review-bot"},
    {"name": "compose-opcert-1", "mem": "4096KiB / 7.664GiB", "compose": "opcert"},
]


def test_host_memory_publishes_the_measured_percentages(tmp_path: Path) -> None:
    """The happy path publishes a MEASURED value, computed from `available`, not `free`."""
    env, aws_log = _environment(tmp_path, free_body=f"cat <<'EOF'\n{_FREE_8GIB_2AVAIL}EOF\n")

    _run(env)

    assert _values(aws_log, "mem_available_percent") == [25]
    assert _values(aws_log, "mem_used_percent") == [75]
    assert _values(aws_log, "mem_probe_ok") == [1]


def test_available_is_read_not_free_column(tmp_path: Path) -> None:
    """ANTI-VACUITY: `free` (col 4) is 6.25% here and `available` (col 7) is 25%.

    A probe that read the wrong column would report a healthy, cache-warm box as nearly
    out of memory — the exact misreading that would send the next OOM investigation after
    the wrong number.
    """
    env, aws_log = _environment(tmp_path, free_body=f"cat <<'EOF'\n{_FREE_8GIB_2AVAIL}EOF\n")

    _run(env)

    assert _values(aws_log, "mem_available_percent") == [25]


@pytest.mark.parametrize(
    ("free_body", "why"),
    [
        ("exit 127", "`free` is not installed on the host"),
        ("exit 1", "`free` ran and failed"),
        ("printf 'garbage\\n'; exit 0", "`free` produced output with no Mem: row"),
        (
            "printf 'Mem: 0 0 0 0 0 0\\n'; exit 0",
            "`free` reported a zero total (an unusable reading)",
        ),
    ],
)
def test_a_failed_memory_read_publishes_pessimistically_not_nothing(
    tmp_path: Path, free_body: str, why: str
) -> None:
    """The regression guard: silence is the one thing this section may not emit.

    ``rebar/host`` alarms treat missing data as breaching on the stated ground that these
    publishers are heartbeats (ticket bff5-9163-cddd-4158). A memory probe that published
    only when it could read leaves a future alarm with no datapoint and makes a dead probe
    look exactly like a healthy box. The pessimistic value is published instead, and
    ``mem_probe_ok=0`` is what keeps that synthesised reading out of the analysis this
    measurement campaign exists to produce.
    """
    env, aws_log = _environment(tmp_path, free_body=free_body)

    _run(env)

    assert _values(aws_log, "mem_available_percent") == [0], (
        f"{why}, and the probe published {_values(aws_log, 'mem_available_percent')} for "
        "mem_available_percent; it must publish exactly [0]."
    )
    assert _values(aws_log, "mem_used_percent") == [100]
    assert _values(aws_log, "mem_probe_ok") == [0]


def test_container_rss_publishes_one_datapoint_per_container(tmp_path: Path) -> None:
    """One metric NAME carrying a dimension, per the ``mount`` precedent — and the
    dimension VALUE is the service, resolved from whichever label the container carries."""
    env, aws_log = _environment(
        tmp_path,
        free_body=f"cat <<'EOF'\n{_FREE_8GIB_2AVAIL}EOF\n",
        docker_body=_docker(_BOX_ROWS),
    )

    _run(env)

    assert _dimensioned(aws_log, "container_memory_rss_bytes") == {
        "gerrit": [3758096384],
        "mcp": [536870912],
        "review-bot": [134742016],
        "opcert": [4194304],
    }
    assert _values(aws_log, "container_stats_ok") == [1]
    assert _values(aws_log, "container_stats_unparsed_rows") == [0]


def test_two_deploys_of_the_same_service_collapse_to_one_dimension_value(
    tmp_path: Path,
) -> None:
    """THE CARDINALITY GUARD, and the reason this dimension is not the container name.

    autodeploy names each blue-green mcp container
    ``${MCP_CONTAINER_PREFIX}-${TARGET:0:12}-${port}`` — a per-deploy commit SHA and a
    blue/green port, both of which move — so the name changes on EVERY deploy. The two
    names below are real ones observed on the box while bug 9ea3-7d07-ea55-4496 was open.
    Keyed on that name, CloudWatch would mint a new dimension value per
    commit: custom-metric cardinality growing without bound (billed per unique dimension
    combination), and, fatally, a per-container series that RESTARTS at every deploy. The
    campaign this section exists to run asks "what is peak mcp RSS during a gate", which is
    a comparison across gate runs; a series that resets whenever a commit lands cannot
    answer it. Two different container names, one dimension value.
    """
    older, newer = "rebar-mcp-3e04025b684e-8092", "rebar-mcp-8db373933654-8093"
    env, aws_log = _environment(
        tmp_path,
        free_body=f"cat <<'EOF'\n{_FREE_8GIB_2AVAIL}EOF\n",
        docker_body=_docker(
            [
                {"name": older, "mem": "512MiB / 7.664GiB", "rebar": "mcp"},
                {"name": newer, "mem": "1GiB / 7.664GiB", "rebar": "mcp"},
            ]
        ),
    )

    _run(env)

    published = _dimensioned(aws_log, "container_memory_rss_bytes")
    assert sorted(published) == ["mcp"], (
        f"two mcp container names ({older}, {newer}) must publish under ONE dimension "
        f"value; got {sorted(published)}"
    )
    # Both readings survive under that one value — a blue-green cutover briefly runs two
    # containers, and `Maximum by service` is what a peak-RSS question asks for.
    assert sorted(published["mcp"]) == [536870912, 1073741824]


def test_an_unlabelled_container_is_bucketed_not_named(tmp_path: Path) -> None:
    """A container carrying NEITHER label is bounded too.

    An mcp container that predates the ``rebar.service`` stamp, or anything hand-run, has
    no service identity to read. Falling back to its own name would reintroduce exactly the
    unbounded cardinality the label exists to prevent, so it lands in a single constant
    bucket — visible in the data, self-healing at the next deploy — and its raw name goes
    to journald, where cardinality is free.
    """
    env, aws_log = _environment(
        tmp_path,
        free_body=f"cat <<'EOF'\n{_FREE_8GIB_2AVAIL}EOF\n",
        docker_body=_docker(
            [
                {"name": "rebar-mcp-3e04025b684e-8092", "mem": "512MiB / 7.664GiB"},
                {"name": "rebar-mcp-8db373933654-8093", "mem": "256MiB / 7.664GiB"},
            ]
        ),
    )

    _run(env)

    published = _dimensioned(aws_log, "container_memory_rss_bytes")
    assert sorted(published) == ["unlabeled"], (
        "an unlabelled container must not publish its own name as a dimension value; got "
        f"{sorted(published)}"
    )
    assert sorted(published["unlabeled"]) == [268435456, 536870912]


def test_rebar_service_wins_over_the_compose_label(tmp_path: Path) -> None:
    """An explicit stamp overrides a compose default, so the identity is always declarable."""
    env, aws_log = _environment(
        tmp_path,
        free_body=f"cat <<'EOF'\n{_FREE_8GIB_2AVAIL}EOF\n",
        docker_body=_docker(
            [
                {
                    "name": "compose-mcp-1",
                    "mem": "512MiB / 7.664GiB",
                    "rebar": "mcp",
                    "compose": "mcp-boot-backend",
                }
            ]
        ),
    )

    _run(env)

    assert sorted(_dimensioned(aws_log, "container_memory_rss_bytes")) == ["mcp"]


def test_an_unparseable_row_is_counted_not_silently_dropped(tmp_path: Path) -> None:
    """A row the census cannot parse must not vanish behind a flag that says it went fine.

    The first cut of this section dropped an unrecognised ``MemUsage`` with a bare ``next``
    while ``container_stats_ok`` still went to 1 off any other parseable row: a container
    present in ``docker stats`` disappeared from the data under a signal claiming health it
    had not observed — the same defect class the whole ticket is about. The drops are their
    own published count instead.

    ``container_stats_ok`` deliberately stays 1 here: it answers "did the census run at
    all", which is what separates a wedged daemon from an idle box, and folding row-level
    parse quality into it would make one weird row look identical to a dead docker.
    """
    env, aws_log = _environment(
        tmp_path,
        free_body=f"cat <<'EOF'\n{_FREE_8GIB_2AVAIL}EOF\n",
        docker_body=_docker(
            [
                {"name": "compose-gerrit-1", "mem": "3.5GiB / 7.664GiB", "compose": "gerrit"},
                # An unrecognised unit, a docker that reported nothing, and a row with no
                # figure at all. The last is the sharpest: read as 0 it would publish a
                # healthy-looking zero-byte container.
                {"name": "compose-opcert-1", "mem": "12PiB / 7.664GiB", "compose": "opcert"},
                {"name": "compose-review-bot-1", "mem": "-- / --", "compose": "review-bot"},
                {"name": "rebar-mcp-3e04025b684e-8092", "mem": "MiB / 7.664GiB", "rebar": "mcp"},
            ]
        ),
    )

    _run(env)

    assert _dimensioned(aws_log, "container_memory_rss_bytes") == {"gerrit": [3758096384]}
    assert _values(aws_log, "container_stats_unparsed_rows") == [3], (
        "every row the census could not parse must be counted, not swallowed"
    )
    assert _values(aws_log, "container_stats_ok") == [1]


def test_docker_stats_is_bounded_by_no_stream_and_timeout(tmp_path: Path) -> None:
    """The probe must never wedge the 5-minute timer on a loaded box.

    ``docker stats`` streams by default and can block on the daemon indefinitely — most
    likely under exactly the memory pressure this metric exists to catch. This stub asserts
    both bounds are actually applied: it HANGS unless invoked through ``timeout`` and with
    ``--no-stream``, so a regression shows up as a test that times out rather than one that
    quietly measures an unbounded call. The ``ps`` join is bounded by ``timeout`` too.
    """
    env, aws_log = _environment(
        tmp_path,
        free_body=f"cat <<'EOF'\n{_FREE_8GIB_2AVAIL}EOF\n",
        docker_body=_docker(
            [{"name": "rebar-mcp-3e04025b684e-8092", "mem": "1GiB / 7.664GiB", "rebar": "mcp"}],
            prelude=(
                '[ -n "${TIMEOUT_APPLIED:-}" ] || sleep 300\n'
                'case "$1:$*" in\n'
                "  stats:*--no-stream*) ;;\n"
                "  stats:*) sleep 300 ;;   # unbounded stream: the wedge this bound prevents\n"
                "esac\n"
            ),
        ),
    )
    # The timeout stub marks its own invocation, so "was `timeout` used at all" is
    # observable from inside the docker stub.
    _stub(
        tmp_path / "bin",
        "timeout",
        'shift; TIMEOUT_APPLIED=1 exec "$@"',
    )

    _run(env)

    assert _dimensioned(aws_log, "container_memory_rss_bytes") == {"mcp": [1073741824]}


@pytest.mark.parametrize(
    ("docker_body", "with_timeout", "why"),
    [
        ("exit 1", True, "the docker daemon rejected the call"),
        ("exit 127", True, "docker is not installed"),
        ("exit 124", True, "`timeout` killed a wedged `docker stats`"),
        (
            _docker([{"name": "compose-gerrit-1", "mem": "3.5GiB / 7.6GiB", "compose": "gerrit"}]),
            False,
            "`timeout` is not on the host",
        ),
    ],
)
def test_a_failed_container_census_publishes_its_heartbeat(
    tmp_path: Path, docker_body: str, with_timeout: bool, why: str
) -> None:
    """Without ``container_stats_ok``, "the census wedged" and "nothing is running" are the
    same observation — no per-container datapoints at all. The per-container gauge cannot
    carry a heartbeat itself, because its dimension set is only knowable from a census that
    succeeded, so the census publishes its own.

    The final case also pins the ``timeout``-is-missing behaviour: the command fails
    rather than running unbounded, and the probe reports that instead of hanging.
    """
    env, aws_log = _environment(
        tmp_path,
        free_body=f"cat <<'EOF'\n{_FREE_8GIB_2AVAIL}EOF\n",
        docker_body=docker_body,
        with_timeout=with_timeout,
    )

    _run(env)

    assert _values(aws_log, "container_stats_ok") == [0], why
    assert _dimensioned(aws_log, "container_memory_rss_bytes") == {}
    # The drop counter is a heartbeat too: published on every tick, including this one.
    assert _values(aws_log, "container_stats_unparsed_rows") == [0]
    # The host gauges are independent of the container census and still measured.
    assert _values(aws_log, "mem_probe_ok") == [1]
