"""In-process ``rebar metrics`` — render the full metric registry over a range.

This is the capstone read command (ticket 9a5a). It hydrates the declarative
metric registry by importing the :mod:`rebar.metrics` **package** (whose
``__init__`` imports the reader modules — ``event_metrics`` / ``git_metrics`` /
``sidecar_metrics`` — that register their specs into ``REGISTRY`` as an import
side effect; importing only ``rebar.metrics.registry`` does NOT trigger them).
It then evaluates every registered spec against a small context object carrying
the resolved store root and the ``--since``/``--until`` bounds, composing each
output entry from BOTH the spec (``lens``) and the evaluate result
(value/source/confidence, or reason/accruing_since).

Each ``evaluate`` call is wrapped in a fault-isolation ``try/except`` so a single
misbehaving metric renders as ``unavailable`` rather than crashing the whole
report — every registered id always appears.

Provenance/adapter isolation: this module NEVER imports any
``rebar.metrics.adapters`` submodule.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from rebar import config
from rebar._engine_support.output import OutputFormatError, parse_output

_USAGE = "Usage: rebar metrics [--since <date>] [--until <date>] [--output json|text]\n"
_DEFAULT_WINDOW_DAYS = 30


def _default_date_range(*, today: date | None = None) -> tuple[str, str]:
    """Return the default inclusive reporting dates for the last 30 days.

    ``today`` is an explicit clock seam for deterministic callers and tests. The
    real CLI uses the current UTC date so its output is stable across host time
    zones.
    """

    until = today or datetime.now(timezone.utc).date()
    since = until - timedelta(days=_DEFAULT_WINDOW_DAYS)
    return since.isoformat(), until.isoformat()


def _parse_dated_flag(argv: list[str], flag: str) -> tuple[str | None, list[str] | None]:
    """Pull ``--flag <value>`` / ``--flag=<value>`` out of ``argv``.

    Returns ``(value, remaining)``; ``(None, None)`` signals a malformed use
    (missing value) after the caller has emitted the usage error.
    """
    rest: list[str] = []
    value: str | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == flag:
            if i + 1 >= len(argv):
                sys.stderr.write(f"Error: {flag} requires a value\n")
                sys.stderr.write(_USAGE)
                return None, None
            value = argv[i + 1]
            i += 2
            continue
        if tok.startswith(flag + "="):
            value = tok[len(flag) + 1 :]
            i += 1
            continue
        rest.append(tok)
        i += 1
    return value, rest


def _entry_for(spec, ctx) -> dict:
    """Compose one output entry from a spec + its (fault-isolated) evaluate result."""
    import rebar.metrics

    try:
        result = rebar.metrics.registry.evaluate(spec, ctx)
    except Exception as exc:  # noqa: BLE001 - fault isolation: one metric must not crash the report
        reason = str(exc) or exc.__class__.__name__
        return {"unavailable": {"reason": reason, "accruing_since": None}}

    if isinstance(result, rebar.metrics.registry.Unavailable):
        return {
            "unavailable": {
                "reason": result.reason,
                "accruing_since": result.accruing_since,
            }
        }
    # A MetricValue: lens rides from the spec; value/source/confidence from the result.
    return {
        "lens": spec.lens,
        "source": result.source,
        "confidence": result.confidence,
        "value": result.value,
    }


def _render_text(since: str, until: str, entries: dict) -> str:
    """One line per metric in registry order: ``<id>  [<lens>/<source>]  <value>``."""
    lines: list[str] = []
    for mid, entry in entries.items():
        if "unavailable" in entry:
            reason = entry["unavailable"]["reason"]
            lines.append(f"{mid}  [unavailable]  unavailable: {reason}")
        else:
            lines.append(f"{mid}  [{entry['lens']}/{entry['source']}]  {entry['value']}")
    return "\n".join(lines) + ("\n" if lines else "")


def metrics_cli(argv: list[str], *, repo_root: str | None = None) -> int:
    """``rebar metrics [--since <d>] [--until <d>] [--output json|text]``."""
    try:
        fmt, rest = parse_output(argv, allowed=("json", "text"), default="json")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        sys.stderr.write(_USAGE)
        return 2

    since, rest2 = _parse_dated_flag(rest, "--since")
    if rest2 is None:
        return 2
    until, rest3 = _parse_dated_flag(rest2, "--until")
    if rest3 is None:
        return 2
    if rest3:
        sys.stderr.write(f"Error: unexpected argument(s): {' '.join(rest3)}\n")
        sys.stderr.write(_USAGE)
        return 2

    default_since, default_until = _default_date_range()
    since = since if since and since.strip() else default_since
    until = until if until and until.strip() else default_until

    root = str(config.repo_root(repo_root) if repo_root is not None else config.repo_root())
    code_health = config.load_config(root).code_health
    ctx = SimpleNamespace(
        repo_root=root,
        since=since,
        until=until,
        scan_roots=code_health.scan_roots,
        size_cap=code_health.size_cap,
        size_near_fraction=code_health.size_near_fraction,
        analysis_cache={},
    )

    # Hydrate REGISTRY: importing the PACKAGE runs the reader modules' registration
    # side effects. Importing only ``rebar.metrics.registry`` would leave it empty.
    import rebar.metrics

    entries: dict = {}
    for spec in rebar.metrics.registry.REGISTRY:
        entries[spec.id] = _entry_for(spec, ctx)

    if fmt == "text":
        sys.stdout.write(_render_text(since, until, entries))
        return 0

    doc = {"since": since, "until": until, "metrics": entries}
    sys.stdout.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return 0
