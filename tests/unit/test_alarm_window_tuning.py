"""CloudWatch alarm windows must not turn a publish gap into a page [rebar:a9d1-c7f3-cfd9-44ff].

Twenty-three ``rebar/host`` and ``Rebar/Gate`` alarms shipped the same shape::

    period = 300  evaluation_periods = 3  datapoints_to_alarm = 2  treat_missing_data = "breaching"

With ``breaching`` an empty period IS a breaching datapoint, so ``2`` of ``3`` alarmed on two
empty buckets alone — no reading of any kind. Measured 2026-09-05: 19 alarms in ALARM, ~17 false.
The same shape also made an alarm UNCLEARABLE whenever the publisher was slower than the window,
because every evaluation then re-supplied the two missing datapoints:
``rebar-docker-buildkit-cache-high`` held ALARM for 10.5 hours while its cache went from 9% over
budget to 0 and published 0.

The publisher has no 5-minute guarantee to lean on. ``infra/scripts/install-observability.sh``
sets ``OnUnitActiveSec=5min`` — measured from the last COMPLETED run — with
``TimeoutStartSec=240``, so the contractual inter-arrival is 5-9 minutes against 5-minute
buckets and empty buckets are structural, not jitter.

This is the offline text contract that keeps the tuning from drifting back, following
``tests/unit/test_alarm_actions_terraform.py``. Three invariants, derived in
``infra/runbooks/alarm-window-tuning.md``:

``I1``  ``treat_missing_data = "breaching"`` requires ``datapoints_to_alarm == evaluation_periods``.
        Missing buckets can then never out-vote a real datapoint, which is what makes both the
        false page and the unclearable state unreachable.
``I2``  A ``breaching`` alarm's window must span at least 900 s, so a healthy publisher is
        guaranteed a datapoint inside it at the 9-minute bound.
``I3``  A non-``breaching`` alarm must budget at least 600 s of publisher time per required
        datapoint, or it can never fire at all. An alarm that cannot fire is as useless as one
        that always fires.

Every invariant is seeded with the exact pre-fix configuration it rejects, so a guard that
silently stopped matching fails instead of passing vacuously.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_TF_DIR = Path(__file__).resolve().parents[2] / "infra" / "terraform"

# Namespaces whose publisher is the host observability probe, i.e. the ones with no cadence
# guarantee. AWS-published metrics (AWS/EC2, AWS/Bedrock) are out of scope by construction.
_PROBE_NAMESPACES = frozenset({"rebar/host", "Rebar/Gate"})

# The smallest window a "breaching" alarm may use. It must comfortably outlast BOTH the 540s
# contractual bound and the largest gap actually measured (10.0 min, twice in two hours), or a
# healthy publisher can empty the whole window and M == N is satisfied by silence alone. The
# contractual bound is 540s (install-observability.sh: OnUnitActiveSec=5min measured from the
# last COMPLETED run, plus TimeoutStartSec=240); 900s is the next 300s multiple above the 600s
# measured worst case, i.e. that worst case plus one period of margin.
_MIN_BREACHING_WINDOW_SECONDS = 900
# 600 s is that bound plus margin, and matches the largest gap measured on the ticket (10.0 min).
_PUBLISHER_BUDGET_PER_DATAPOINT_SECONDS = 600

# The tree had 30 alarms, 26 of them probe-published, when this guard was written. The floors
# exist so a parser that silently matches NOTHING fails loudly instead of passing for free.
_MIN_EXPECTED_ALARMS = 30
_MIN_EXPECTED_PROBE_ALARMS = 26

_ALARM_RE = re.compile(r'resource\s+"aws_cloudwatch_metric_alarm"\s+"(?P<name>[^"]+)"\s*\{')


class Alarm:
    """One parsed ``aws_cloudwatch_metric_alarm`` block."""

    def __init__(self, file_name: str, label: str, body: str) -> None:
        self.file_name = file_name
        self.label = label
        self.namespace = _quoted(body, "namespace") or ""
        self.treat_missing_data = _quoted(body, "treat_missing_data") or "missing"
        self.period = _integer(body, "period")
        self.evaluation_periods = _integer(body, "evaluation_periods")
        explicit = _integer(body, "datapoints_to_alarm")
        # An unset datapoints_to_alarm is N-of-N in CloudWatch, not a hole.
        self.datapoints_to_alarm = self.evaluation_periods if explicit is None else explicit

    @property
    def probe_published(self) -> bool:
        return self.namespace in _PROBE_NAMESPACES

    @property
    def window_seconds(self) -> int:
        return (self.period or 0) * (self.evaluation_periods or 0)

    def __str__(self) -> str:
        return (
            f"{self.file_name}:{self.label} period={self.period} "
            f"evaluation_periods={self.evaluation_periods} "
            f"datapoints_to_alarm={self.datapoints_to_alarm} "
            f"treat_missing_data={self.treat_missing_data}"
        )


def _quoted(body: str, attribute: str) -> str | None:
    """Value of a quoted top-level attribute, or ``None`` when unset."""
    match = re.search(rf'^[ \t]*{attribute}[ \t]*=[ \t]*"([^"]+)"', body, re.MULTILINE)
    return match.group(1) if match else None


def _integer(body: str, attribute: str) -> int | None:
    """Value of an integer top-level attribute, or ``None`` when unset."""
    match = re.search(rf"^[ \t]*{attribute}[ \t]*=[ \t]*(\d+)", body, re.MULTILINE)
    return int(match.group(1)) if match else None


def _parse(file_name: str, source: str) -> list[Alarm]:
    """Every alarm block in one Terraform source, brace-matched from its header."""
    alarms: list[Alarm] = []
    for match in _ALARM_RE.finditer(source):
        start = match.end()
        depth, index = 1, start
        while depth and index < len(source):
            depth += {"{": 1, "}": -1}.get(source[index], 0)
            index += 1
        alarms.append(Alarm(file_name, match.group("name"), source[start : index - 1]))
    return alarms


def _tree_alarms() -> list[Alarm]:
    """Every alarm committed under ``infra/terraform/``."""
    alarms: list[Alarm] = []
    for path in sorted(_TF_DIR.glob("*.tf")):
        alarms.extend(_parse(path.name, path.read_text(encoding="utf-8")))
    return alarms


# ─────────────────────────────── anti-vacuity ────────────────────────────────


def test_the_parser_finds_the_alarms_it_is_meant_to_guard() -> None:
    """A guard that matches nothing passes for free. This is the floor that stops that."""
    alarms = _tree_alarms()
    assert len(alarms) >= _MIN_EXPECTED_ALARMS, f"parsed only {len(alarms)} alarms"
    probe = [alarm for alarm in alarms if alarm.probe_published]
    assert len(probe) >= _MIN_EXPECTED_PROBE_ALARMS, f"parsed only {len(probe)} probe alarms"
    assert all(alarm.period and alarm.evaluation_periods for alarm in probe)


# ────────────────────── I1 + I2: no unclearable alarms ───────────────────────


def test_breaching_alarms_require_every_period_in_the_window() -> None:
    """``breaching`` with ``M < N`` lets missing buckets out-vote a real datapoint.

    That is both halves of the defect at once: two empty buckets page on their own, and a
    publisher slower than the window re-supplies them on every evaluation, so no value the
    metric can publish clears the alarm. ``M == N`` removes both — a page then needs the WHOLE
    window breaching or empty, and one healthy datapoint always ends it.
    """
    offenders = [
        str(alarm)
        for alarm in _tree_alarms()
        if alarm.probe_published
        and alarm.treat_missing_data == "breaching"
        and alarm.datapoints_to_alarm != alarm.evaluation_periods
    ]
    assert not offenders, (
        "alarm(s) treat missing data as breaching with datapoints_to_alarm < "
        "evaluation_periods, so an ordinary publish gap can page on its own and a slow "
        "publisher can hold them in ALARM forever: " + "; ".join(offenders) + ". Set "
        "datapoints_to_alarm = evaluation_periods, or move the alarm to notBreaching if an "
        "INTERMITTENT breach is its real condition (infra/runbooks/alarm-window-tuning.md)."
    )


def test_breaching_windows_outlast_a_publish_gap() -> None:
    """``M == N`` is only safe while a healthy publisher is guaranteed a datapoint inside N.

    The probe's contractual inter-arrival is 5-9 minutes, so a window shorter than that can be
    entirely empty during healthy operation and ``M == N`` would be satisfied by silence alone.
    """
    offenders = [
        str(alarm)
        for alarm in _tree_alarms()
        if alarm.probe_published
        and alarm.treat_missing_data == "breaching"
        and alarm.window_seconds < _MIN_BREACHING_WINDOW_SECONDS
    ]
    assert not offenders, (
        "breaching alarm window(s) are too short to outlast an ordinary publish gap, so "
        "healthy operation can empty the whole window and silence alone satisfies M == N: "
        + "; ".join(offenders)
        + f". Widen evaluation_periods until period * evaluation_periods >= "
        f"{_MIN_BREACHING_WINDOW_SECONDS}s."
    )


# ───────────────────────── I3: no unfireable alarms ──────────────────────────


def test_non_breaching_alarms_can_actually_reach_their_datapoint_count() -> None:
    """An alarm that cannot fire is as useless as one that always fires.

    Once missing periods stop counting toward ``datapoints_to_alarm``, every required datapoint
    has to genuinely arrive inside the window. At a 5-9 minute publish cadence that means the
    window must budget at least 600 s per required datapoint, or the alarm is decorative.
    """
    offenders = []
    for alarm in _tree_alarms():
        if not alarm.probe_published or alarm.treat_missing_data == "breaching":
            continue
        budget = alarm.window_seconds / alarm.datapoints_to_alarm
        if budget < _PUBLISHER_BUDGET_PER_DATAPOINT_SECONDS:
            offenders.append(f"{alarm} budget={budget:.0f}s")
    assert not offenders, (
        "alarm(s) require more real datapoints than the publisher can deliver inside the "
        "window, so they can never fire: " + "; ".join(offenders) + ". Widen "
        "evaluation_periods rather than lowering datapoints_to_alarm."
    )


# ───────────── defect seeding: the exact pre-fix configurations ──────────────

_SEED_HEADER = 'resource "aws_cloudwatch_metric_alarm" "seeded" {\n  namespace = "rebar/host"\n'

_PRE_FIX_MIRROR = _SEED_HEADER + (
    "  period              = 300\n"
    "  evaluation_periods  = 3\n"
    "  datapoints_to_alarm = 2\n"
    '  treat_missing_data  = "breaching"\n}\n'
)


def _seeded(source: str) -> Alarm:
    alarms = _parse("seeded.tf", source)
    assert len(alarms) == 1
    return alarms[0]


def test_the_pre_fix_shape_violates_the_unclearable_guard() -> None:
    """300/3/2/breaching — the shape that put 19 alarms in ALARM — must be rejected by I1."""
    alarm = _seeded(_PRE_FIX_MIRROR)
    assert alarm.probe_published
    assert alarm.datapoints_to_alarm != alarm.evaluation_periods


def test_the_pre_fix_gate_down_shape_violates_the_window_guard() -> None:
    """300/2 breaching — gate-down's old shape — is a 600s window, and 10.0-minute publish
    gaps were measured TWICE inside two hours, so healthy operation could empty it outright."""
    alarm = _seeded(
        _SEED_HEADER + "  period = 300\n  evaluation_periods = 2\n"
        '  treat_missing_data = "breaching"\n}\n'
    )
    assert alarm.datapoints_to_alarm == 2, "unset datapoints_to_alarm must read as N-of-N"
    assert alarm.window_seconds < _MIN_BREACHING_WINDOW_SECONDS


def test_an_unreachable_datapoint_count_violates_the_fireability_guard() -> None:
    """notBreaching with a window too tight for its datapoints can never fire."""
    alarm = _seeded(
        _SEED_HEADER + "  period = 300\n  evaluation_periods = 3\n"
        '  datapoints_to_alarm = 3\n  treat_missing_data = "notBreaching"\n}\n'
    )
    assert alarm.window_seconds / alarm.datapoints_to_alarm < (
        _PUBLISHER_BUDGET_PER_DATAPOINT_SECONDS
    )


def test_aws_published_alarms_are_out_of_scope() -> None:
    """AWS publishes AWS/EC2 on a guaranteed 60s cadence; no gap analysis applies to it."""
    alarm = _seeded(
        'resource "aws_cloudwatch_metric_alarm" "seeded" {\n  namespace = "AWS/EC2"\n'
        "  period = 60\n  evaluation_periods = 2\n}\n"
    )
    assert not alarm.probe_published


# ───────────────────── the fix itself, pinned by name ────────────────────────


def test_the_liveness_sentinels_still_treat_silence_as_breaching() -> None:
    """Dead-publisher detection must survive moving the counters to notBreaching.

    These seven heartbeats publish a value on EVERY run and are spread from observability.sh §1
    to §5, so between them they page for a probe that stops AND for a run truncated by the
    unit's 240s TimeoutStartSec. Losing them to quieten the noise would re-introduce the
    inversion ticket bff5-9163-cddd-4158 fixed.
    """
    sentinels = {
        ("monitoring.tf", "gerrit_gate_down"),
        ("monitoring_9ea3.tf", "mcp_serving_path_down"),
        ("monitoring_autodeploy.tf", "gate_scratch_unmounted"),
        ("monitoring_autodeploy.tf", "journal_cap_not_in_effect"),
        ("monitoring_autodeploy.tf", "var_tmp_cleanup_not_active"),
        ("monitoring_autodeploy.tf", "container_reaper_not_active"),
        ("monitoring_ws7.tf", "mirror_out_of_sync"),
    }
    found = {
        (alarm.file_name, alarm.label): alarm.treat_missing_data
        for alarm in _tree_alarms()
        if (alarm.file_name, alarm.label) in sentinels
    }
    assert set(found) == sentinels, f"sentinel(s) renamed or removed: {sentinels - set(found)}"
    assert all(value == "breaching" for value in found.values()), found


def test_the_reconstructed_firing_window_no_longer_pages() -> None:
    """AC1: `1, MISSING, 1` — one divergence sample plus one gap — must not alarm.

    The mirror alarm's page now needs every period in an 8-period window breaching or empty. The
    probe publishes 0 on the runs between two independent sub-minute catches of fresh submits,
    and with ``M == N`` any single 0 clears the window.
    """
    mirror = next(alarm for alarm in _tree_alarms() if alarm.label == "mirror_out_of_sync")
    breaching_samples = 2  # the two `1`s of `1, MISSING, 1`
    assert mirror.treat_missing_data == "breaching"
    assert mirror.datapoints_to_alarm == mirror.evaluation_periods
    assert breaching_samples < mirror.datapoints_to_alarm


# ───────────── dead-man coverage for the notBreaching counters ───────────────
#
# Moving the nine error counters to ``notBreaching`` makes "the publisher is dead" and "there
# were no errors" the SAME observation on those metrics. That is only safe if some OTHER alarm
# still pages when the publisher dies, and the coverage has to be PROVEN, not assumed.
#
# The early heartbeats do NOT provide it. Measured over 8 h on 2026-09-05, ``mcp_healthy`` (§1b)
# and ``gate_scratch_mounted`` (§2e) were present in 54 buckets where ``g2p_dispatch_errors`` was
# absent — they demonstrably do not stop together, because the probe is truncated between them.
#
# Coverage comes from the TAIL sentinel instead. ``mirror_out_of_sync`` is the LAST metric the
# probe publishes, and publication is sequential, so reaching its line implies every counter's
# line was reached: sentinel silence is a superset of counter silence. Measured, that superset
# holds to within 3 buckets of 41 (0 for the counter published immediately before it).
#
# The ordering is therefore load-bearing, and this is the test that keeps it true. Moving
# ``mirror_out_of_sync`` earlier, or adding a counter after it, silently removes the dead-man
# from nine alarms — so it fails the build here instead.

_OBSERVABILITY = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"

_TAIL_SENTINEL_METRIC = "mirror_out_of_sync"

# The nine alarms that gave up their own dead-man, keyed by the metric each one watches.
_NOTBREACHING_COUNTER_METRICS = (
    "replication_errors",
    "voter_errors",
    "review_bot_merge_change_errors",
    "deploy_errors",
    "review_interrupts_bound_exceeded",
    "review_interrupts_signal_unavailable",
    "mcp_retire_cap",
    "mcp_mem_abort",
    "g2p_dispatch_errors",
)


def _publish_line(source: str, metric: str) -> int | None:
    """1-based line at which the probe emits this metric.

    Two shapes exist. Most sections call ``put-metric-data --metric-name <name>`` directly. The
    autodeploy marker counters go through ``publish_autodeploy_marker_delta``, which takes the
    name as a positional argument and passes it on as ``--metric-name "$metric"``, so at the
    call site the literal name appears as a bare shell word. Comments are skipped, because every
    one of these names is also discussed in prose above its section.
    """
    direct = re.compile(rf"--metric-name[ \t]+{re.escape(metric)}\b")
    word = re.compile(rf"(?<![\w-]){re.escape(metric)}(?![\w-])")
    fallback: int | None = None
    for index, line in enumerate(source.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        if direct.search(line):
            return index
        if fallback is None and word.search(line):
            fallback = index
    return fallback


def test_the_tail_sentinel_is_published_after_every_notbreaching_counter() -> None:
    """The dead-man that covers the nine notBreaching counters must publish AFTER all of them.

    Publication is sequential, so this ordering is exactly the coverage claim: if the sentinel's
    datapoint exists, the probe reached every counter above it, and if the probe died the
    sentinel is silent and pages. Reverse the order and nine alarms lose their dead-man with no
    other symptom.
    """
    source = _OBSERVABILITY.read_text(encoding="utf-8")
    sentinel_line = _publish_line(source, _TAIL_SENTINEL_METRIC)
    assert sentinel_line is not None, (
        f"{_TAIL_SENTINEL_METRIC} is no longer published by observability.sh; the nine "
        "notBreaching counters in Profile B have lost their dead-man entirely"
    )
    later = []
    for metric in _NOTBREACHING_COUNTER_METRICS:
        line = _publish_line(source, metric)
        assert line is not None, f"{metric} is no longer published; its alarm cannot fire"
        if line > sentinel_line:
            later.append(f"{metric}@{line}")
    assert not later, (
        f"counter(s) are published AFTER the tail sentinel {_TAIL_SENTINEL_METRIC}@"
        f"{sentinel_line}: {', '.join(later)}. A run truncated between the sentinel and them "
        "would leave those counters silent while the sentinel still publishes, so their "
        "notBreaching alarms would read healthy while the publisher was dead. Either move the "
        "sentinel back to last, or give those counters their own liveness signal "
        "(infra/runbooks/alarm-window-tuning.md)."
    )


def test_the_tail_sentinel_alarm_is_a_dead_man() -> None:
    """The sentinel only covers anything while it treats silence as breaching at M == N."""
    sentinel = next(alarm for alarm in _tree_alarms() if alarm.label == _TAIL_SENTINEL_METRIC)
    assert sentinel.treat_missing_data == "breaching"
    assert sentinel.datapoints_to_alarm == sentinel.evaluation_periods


def test_the_ordering_guard_rejects_a_counter_published_after_the_sentinel() -> None:
    """ANTI-VACUITY: the line-order helper must actually order these two shapes."""
    seeded = (
        "aws cloudwatch put-metric-data --metric-name mirror_out_of_sync --value 0\n"
        "aws cloudwatch put-metric-data --metric-name voter_errors --value 0\n"
    )
    assert _publish_line(seeded, "mirror_out_of_sync") == 1
    assert _publish_line(seeded, "voter_errors") == 2
