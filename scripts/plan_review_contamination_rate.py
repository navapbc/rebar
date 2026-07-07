#!/usr/bin/env python3
"""Blocking-tier chunk-contamination rate from REVIEW_RESULT sidecar `cohort` data.

Epic cite-stone-sea / WS9 (sip-thorn-epoch). rebar deliberately bin-packs same-facet criteria
into one finder call for cost/cache efficiency, accepting cross-criterion contamination within a
chunk and relying on the independent verifier to catch it. WS9 stamps each finding with its
`cohort` — the sorted set of criterion ids co-resident in the finder call that produced it. This
script measures how often a BLOCKING-tier finding came from a chunk where OTHER criteria were
co-resident (cohort size > 1), i.e. was NOT reviewed in isolation — the empirical signal that
gates the R-1-part-2 blocking-tier isolation work (placeholder epic fort-wisp-wren).

METRIC (defined here so the computation is unambiguous):

    contamination_rate = |{ f : f is blocking-tier AND len(f.cohort) > 1 }|
                         ------------------------------------------------------
                         |{ f : f is blocking-tier AND f.cohort is not None }|

- "blocking-tier" = a finding whose Pass-3 ``decision`` is ``"block"`` (the only tier that can
  block a claim); advisory findings are excluded (contamination on an advisory never blocks).
- A finding with a MISSING cohort (``None`` — written before WS9, or by a path that does not
  stamp it) is treated as **"unknown"** and EXCLUDED from both numerator and denominator (never
  counted as an empty/isolated cohort). The count of unknowns is reported separately so a caller
  can see how much of the corpus is un-analysable rather than silently trusting the rate.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def contamination_rate(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the blocking-tier contamination rate over a flat list of sidecar findings.

    Returns ``{rate, contaminated, blocking_with_cohort, unknown}``. ``rate`` is ``None`` when no
    blocking-tier finding carries a cohort (nothing to measure — do not report 0.0, which would
    read as "measured, none contaminated")."""
    blocking = [f for f in findings if f.get("decision") == "block"]
    with_cohort = [f for f in blocking if f.get("cohort") is not None]
    unknown = len(blocking) - len(with_cohort)  # missing cohort => "unknown", excluded
    contaminated = [f for f in with_cohort if len(f.get("cohort") or []) > 1]
    rate = (len(contaminated) / len(with_cohort)) if with_cohort else None
    return {
        "rate": rate,
        "contaminated": len(contaminated),
        "blocking_with_cohort": len(with_cohort),
        "unknown": unknown,
    }


def _findings_from_sidecars(sidecars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten the `findings` arrays of a list of REVIEW_RESULT sidecar payloads."""
    out: list[dict[str, Any]] = []
    for sc in sidecars:
        out.extend(sc.get("findings", []) or [])
    return out


def main(argv: list[str] | None = None) -> int:
    """Read REVIEW_RESULT sidecar payloads as JSON (one object or a JSON array) from the named
    file or stdin, and print the contamination rate. (A thin CLI over ``contamination_rate``;
    the sidecar corpus itself is produced by the plan-review gate.)"""
    args = list(sys.argv[1:] if argv is None else argv)
    raw = open(args[0]).read() if args else sys.stdin.read()
    data = json.loads(raw)
    sidecars = data if isinstance(data, list) else [data]
    result = contamination_rate(_findings_from_sidecars(sidecars))
    print(json.dumps(result, indent=2))  # noqa: T201 — CLI presentation (a standalone script)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
