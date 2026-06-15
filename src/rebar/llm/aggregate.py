"""Multi-reviewer finding aggregation: cluster → consensus → rank.

When several reviewers review the same change, they surface overlapping findings.
This module merges per-reviewer results into one ranked list: findings about the
same place/dimension are clustered, the cluster's **agreement** (how many reviewers
raised it) is recorded, citations are unioned, and the result is ranked by
severity × agreement. Deterministic and stdlib-only (no embeddings) — clustering
is by file location (path + nearby line) + dimension, falling back to a normalized
detail prefix.
The aggregated findings carry two extra (schema-allowed) fields: ``agreement``
(int) and ``reviewers`` (the reviewer ids that raised it).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
_LINE_BUCKET = 10  # cluster findings within ~10 lines of each other


def _severity_rank(finding: dict) -> int:
    return _SEVERITY_RANK.get(str(finding.get("severity", "info")).lower(), 0)


def _text_key(finding: dict):
    """Fallback cluster key for a finding with no file citation: dimension +
    normalized detail prefix."""
    dim = str(finding.get("dimension", "")).strip().lower()
    detail = " ".join(str(finding.get("detail", "")).lower().split())[:80]
    return (dim, detail)


def _file_anchor(finding: dict):
    """``(path, dim, line|None)`` for the finding's first file citation, else None.
    ``line`` is None for a whole-file citation (line_start 0/omitted)."""
    dim = str(finding.get("dimension", "")).strip().lower()
    for cit in finding.get("citations", []):
        if cit.get("kind") == "file" and cit.get("path"):
            ls = cit.get("line_start")
            line = ls if isinstance(ls, int) and ls > 0 else None
            return (cit["path"], dim, line)
    return None


def _matches(cluster: dict, finding: dict) -> bool:
    """Whether ``finding`` belongs in ``cluster``: same file+dimension within
    ``_LINE_BUCKET`` lines of the cluster's anchor — *proximity*, not a fixed bucket,
    so two findings straddling a bucket boundary (e.g. lines 9 and 11) still cluster
    — or same dimension+detail for text-only findings. A file finding never clusters
    with a text-only one."""
    anchor = _file_anchor(finding)
    if anchor is not None:
        if cluster["pd"] is None:
            return False
        path, dim, line = anchor
        if cluster["pd"] != (path, dim):
            return False
        if line is None or cluster["anchor"] is None:
            return line is None and cluster["anchor"] is None  # both whole-file
        return abs(line - cluster["anchor"]) <= _LINE_BUCKET
    return cluster["pd"] is None and cluster["txt"] == _text_key(finding)


def _iter_reviewer_findings(per_reviewer: Iterable) -> Iterator[tuple[str | None, list[dict]]]:
    """Accept either review_result dicts or (reviewer_id, findings) pairs."""
    for item in per_reviewer:
        if isinstance(item, dict) and "findings" in item:
            rid = (item.get("reviewers") or [None])[0]
            yield rid, item.get("findings") or []
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            yield item[0], list(item[1])


def aggregate_findings(per_reviewer: Iterable) -> list[dict]:
    """Merge per-reviewer findings → one ranked, de-duplicated list.

    ``per_reviewer`` is a list of review_result dicts (one per reviewer) or of
    ``(reviewer_id, findings)`` pairs. Returns findings sorted by severity then
    agreement (descending), each with merged citations + ``agreement``/``reviewers``.
    """
    # A list (not a keyed dict) so clustering can be by line *proximity* to the
    # cluster's anchor rather than a fixed bucket; insertion order is preserved and
    # deterministic for a given input.
    clusters: list[dict] = []
    for reviewer_id, findings in _iter_reviewer_findings(per_reviewer):
        for finding in findings:
            cluster = next((c for c in clusters if _matches(c, finding)), None)
            if cluster is None:
                anchor = _file_anchor(finding)
                cluster = {
                    "pd": (anchor[0], anchor[1]) if anchor else None,
                    "anchor": anchor[2] if anchor else None,
                    "txt": None if anchor else _text_key(finding),
                    "items": [],
                    "reviewers": set(),
                }
                clusters.append(cluster)
            cluster["items"].append(finding)
            if reviewer_id:
                cluster["reviewers"].add(reviewer_id)

    merged: list[dict] = []
    for cluster in clusters:
        # Representative = highest severity, then longest detail (most specific).
        rep = max(
            cluster["items"], key=lambda f: (_severity_rank(f), len(str(f.get("detail", ""))))
        )
        out = dict(rep)
        seen, cits = set(), []
        for finding in cluster["items"]:
            for cit in finding.get("citations", []):
                ckey = json.dumps(cit, sort_keys=True)
                if ckey not in seen:
                    seen.add(ckey)
                    cits.append(cit)
        out["citations"] = cits
        reviewers = sorted(cluster["reviewers"])
        out["reviewers"] = reviewers
        out["agreement"] = len(reviewers) if reviewers else len(cluster["items"])
        merged.append(out)

    merged.sort(key=lambda f: (_severity_rank(f), f.get("agreement", 1)), reverse=True)
    return merged
