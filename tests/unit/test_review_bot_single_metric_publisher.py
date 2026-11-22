"""Every CloudWatch metric has one publisher: review-bot code or the host probe, never both.

The host probe (``infra/scripts/observability.sh``) counts review-bot journal markers and
publishes them as ``rebar/host`` metrics. When review-bot code also published the same metric
through boto3, every error counted twice on the box, because the container can reach the
instance role (bug ab02-9eaa-5b93-4c1f). This test fails if any review-bot module calls
``put_metric_data`` for a metric name the probe also publishes.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from _tree_scan import parsed_python_files

_ROOT = Path(__file__).resolve().parents[2]
_REVIEW_BOT = _ROOT / "src" / "rebar" / "review_bot"
_PROBE = _ROOT / "infra" / "scripts" / "observability.sh"

# Shape 1: ``--metric-name <name>`` (a line continuation may separate the two words).
_DIRECT_RE = re.compile(r"--metric-name[ \t\\\n]+(?P<name>[A-Za-z0-9_]+)")
# Shape 2: ``publish_autodeploy_marker_delta '<regex>' <name> ...``; the helper itself passes
# ``--metric-name "$metric"``, so the literal name is the call site's second word.
_HELPER_RE = re.compile(
    r"^publish_autodeploy_marker_delta[ \t\\\n]+'[^']*'[ \t\\\n]+(?P<name>[A-Za-z0-9_]+)",
    re.MULTILINE,
)


def probe_metric_names(source: str) -> set[str]:
    """Metric names the probe script publishes, from both publishing shapes."""
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    return {m["name"] for m in _DIRECT_RE.finditer(code)} | {
        m["name"] for m in _HELPER_RE.finditer(code)
    }


def _metric_name_values(tree: ast.AST) -> list[ast.expr]:
    """Every value stored under a ``"MetricName"`` key in a dict literal, anywhere in the module.

    Searching the whole module, not just the call's arguments, catches MetricData lists that
    are built before the call (a comprehension or a variable) and then passed in."""
    values: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "MetricName":
                    values.append(value)
    return values


def _calls_put_metric_data(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "put_metric_data"
        for node in ast.walk(tree)
    )


def python_metric_names(tree: ast.AST, where: str) -> tuple[set[str], set[str], list[str]]:
    """Return (exact names, f-string prefixes, unresolvable sites) a module publishes.

    A module that calls ``put_metric_data`` must spell each name as a string literal or an
    f-string with a literal prefix. Anything else (a constant, a parameter) is reported as
    unresolvable, so an empty overlap can never come from a name the scan could not read."""
    exact: set[str] = set()
    prefixes: set[str] = set()
    unresolved: list[str] = []
    if not _calls_put_metric_data(tree):
        return exact, prefixes, unresolved
    values = _metric_name_values(tree)
    if not values:
        unresolved.append(f"{where}: calls put_metric_data but no MetricName literal was found")
    for value in values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            exact.add(value.value)
        elif (
            isinstance(value, ast.JoinedStr)
            and value.values
            and isinstance(value.values[0], ast.Constant)
            and str(value.values[0].value)
        ):
            prefixes.add(str(value.values[0].value))
        else:
            unresolved.append(f"{where}:{value.lineno}: MetricName is not a literal")
    return exact, prefixes, unresolved


def overlaps(exact: set[str], prefixes: set[str], probe: set[str]) -> set[str]:
    """Probe metric names that review-bot code also publishes."""
    return (exact & probe) | {name for name in probe if any(name.startswith(p) for p in prefixes)}


def test_probe_parser_sees_both_publishing_shapes() -> None:
    """The probe parser must find names from both shapes, so an empty overlap below is real."""
    names = probe_metric_names(_PROBE.read_text(encoding="utf-8"))
    # Shape 1 (direct) and shape 2 (helper) representatives.
    assert {"voter_errors", "review_bot_merge_change_errors"} <= names
    assert {"deploy_deferrals", "review_interrupts_bound_exceeded", "mcp_mem_abort"} <= names


def test_scanner_detects_an_overlapping_review_bot_publish() -> None:
    """A synthetic module that publishes voter_errors must be reported as an overlap."""
    direct = ast.parse(
        "import boto3\n"
        "boto3.client('cloudwatch').put_metric_data(Namespace='rebar/host',"
        " MetricData=[{'MetricName': 'voter_errors', 'Value': 1}])\n"
    )
    exact, prefixes, unresolved = python_metric_names(direct, "synthetic")
    assert unresolved == []
    assert overlaps(exact, prefixes, {"voter_errors", "gerrit_healthy"}) == {"voter_errors"}


def test_scanner_detects_names_built_before_the_call() -> None:
    """An f-string name built in a comprehension before the call is still found."""
    built = ast.parse(
        "data = [{'MetricName': f'voter_{k}', 'Value': 1} for k in ('errors',)]\n"
        "client.put_metric_data(Namespace='rebar/host', MetricData=data)\n"
    )
    exact, prefixes, unresolved = python_metric_names(built, "synthetic")
    assert unresolved == []
    assert overlaps(exact, prefixes, {"voter_errors"}) == {"voter_errors"}


def test_scanner_reports_a_name_it_cannot_read() -> None:
    """A name held in a constant is reported as unresolvable, not silently skipped."""
    indirect = ast.parse(
        "NAME = 'voter_errors'\n"
        "client.put_metric_data(Namespace='rebar/host', MetricData=[{'MetricName': NAME}])\n"
    )
    _exact, _prefixes, unresolved = python_metric_names(indirect, "synthetic")
    assert unresolved


def test_review_bot_never_publishes_a_metric_the_host_probe_publishes() -> None:
    """Review-bot code and the host probe must not both publish one metric."""
    probe = probe_metric_names(_PROBE.read_text(encoding="utf-8"))
    offenders: list[str] = []
    unresolved: list[str] = []
    for module in parsed_python_files(_REVIEW_BOT):
        exact, prefixes, bad = python_metric_names(module.tree, str(module.relative))
        unresolved.extend(bad)
        for name in sorted(overlaps(exact, prefixes, probe)):
            offenders.append(f"{module.relative}: {name}")
    assert not unresolved, "metric names the guard cannot read:\n" + "\n".join(unresolved)
    assert not offenders, (
        "review-bot code publishes metrics the host probe already publishes, so each event "
        "would count twice (bug ab02-9eaa-5b93-4c1f):\n" + "\n".join(offenders)
    )
