"""Process-level token-usage sink for the live-LLM CI jobs.

``PydanticAIRunner.run`` already extracts per-call token usage (input/output/cache
tokens + request count) and attaches it as ``result["_usage"]`` (see ``runner.py``).
This module turns that in-memory value into a **durable, retrievable** record for the
two weekly, billable jobs — the external tier (``external-integration.yml``) and the
live prompt-eval (``prompt-eval.yml``) — which otherwise surface no spend at all.

Opt-in via the ``REBAR_USAGE_LOG`` env var: when it points at a path, :func:`record`
appends one JSON object per LLM call (JSONL). :func:`summarize` folds that file into a
Markdown table for ``$GITHUB_STEP_SUMMARY``; the raw JSONL is uploaded as a CI artifact.
When the env var is unset (every normal library/test run) :func:`record` is a **no-op**,
so the default runner path and ``make test`` are byte-unchanged.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

#: Env var naming the JSONL sink file. Unset ⇒ recording is off (the default).
ENV_VAR = "REBAR_USAGE_LOG"

#: The integer token fields ``_extract_usage()`` reports (runner.py); summed by summarize().
_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "requests")


def record(usage: dict, *, op: str) -> None:
    """Append one usage record for a single LLM call to ``$REBAR_USAGE_LOG`` (JSONL).

    No-op when the env var is unset (the default) or ``usage`` is empty. Best-effort: a
    telemetry sink must never break the LLM call path, so any write error is logged and
    swallowed rather than raised into the runner.
    """
    path = os.environ.get(ENV_VAR)
    if not path or not usage:
        return
    row: dict[str, object] = {"op": op}
    for field in _FIELDS:
        row[field] = int(usage.get(field, 0) or 0)
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
    except OSError as exc:  # pragma: no cover - telemetry must not fail a run
        logger.warning("usage-log record failed for op=%s: %s", op, exc)


def _read(path: str) -> list[dict]:
    """Parse the JSONL at ``path``; tolerate a missing file and skip malformed lines."""
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("usage-log: skipping malformed line in %s", path)
    except FileNotFoundError:
        return []
    return rows


def summarize(path: str) -> str:
    """Return a Markdown summary (per-op breakdown + totals) of the JSONL at ``path``.

    A missing or empty file yields exactly ``No LLM calls recorded.`` so a run that made
    zero LLM calls still prints an honest, valid line.
    """
    rows = _read(path)
    if not rows:
        return "No LLM calls recorded."
    per_op: dict[str, dict[str, int]] = {}
    totals = {field: 0 for field in _FIELDS}
    calls = 0
    for row in rows:
        calls += 1
        op = str(row.get("op", "?"))
        agg = per_op.setdefault(op, {field: 0 for field in _FIELDS} | {"calls": 0})
        agg["calls"] += 1
        for field in _FIELDS:
            value = int(row.get(field, 0) or 0)
            agg[field] += value
            totals[field] += value
    lines = [
        "### LLM token usage",
        "",
        "| op | calls | input | output | cache_read | cache_write | requests |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for op in sorted(per_op):
        agg = per_op[op]
        lines.append(
            f"| {op} | {agg['calls']} | {agg['input_tokens']} | {agg['output_tokens']} | "
            f"{agg['cache_read_tokens']} | {agg['cache_write_tokens']} | {agg['requests']} |"
        )
    lines.append(
        f"| **total** | **{calls}** | **{totals['input_tokens']}** | **{totals['output_tokens']}** "
        f"| **{totals['cache_read_tokens']}** | **{totals['cache_write_tokens']}** "
        f"| **{totals['requests']}** |"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m rebar.llm.usage_log summarize <path>`` prints the Markdown summary."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m rebar.llm.usage_log")
    sub = parser.add_subparsers(dest="cmd", required=True)
    summarize_parser = sub.add_parser("summarize", help="print a Markdown token-usage summary")
    summarize_parser.add_argument("path", help="path to the JSONL usage log")
    args = parser.parse_args(argv)
    if args.cmd == "summarize":
        sys.stdout.write(summarize(args.path) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via main() in tests
    raise SystemExit(main())
