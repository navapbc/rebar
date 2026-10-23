#!/usr/bin/env python3
"""Capture scrubbed Jira REST payloads for hermetic snapshot tests.

With explicit ``REBAR_CAPTURE_JIRA_FIXTURES=1`` and ``JIRA_PROJECT``, read Jira
through production ``AcliClient`` methods; never write to Jira. Capture the 4
fetcher inputs: ``search.json``, ``comment_map.json``, ``issuelinks_map.json``,
and ``parent_map.json``. Layered scrubbing removes tokens, emails, and account
IDs; ``FakeAcliClient`` replays the saved shapes through the production fetcher,
and fixture tests check the scrub.

Usage: ``REBAR_CAPTURE_JIRA_FIXTURES=1 JIRA_PROJECT=REB python scripts/capture_jira_fixtures.py``

Refresh when Jira response shape or fetcher enrichment changes, committing
fixtures with the snapshot and fixture contract tests. See docs/jira-fixtures.md."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# A small stable REB set covers nested comments, links (including empty),
# parent links (including null), and a non-null assignee. Refresh by capture.
DEFAULT_KEYS = [
    "REB-407",  # assigned (non-null assignee shape)
    "REB-408",  # comments + 1 link + parent
    "REB-426",  # story C: comments + link + parent (REB-430)
    "REB-427",  # story B: comments + link + parent (REB-430)
    "REB-428",  # story D: comments + link + parent (REB-430)
    "REB-429",  # comments + 2 links + parent (REB-184)
    "REB-430",  # the epic: comments, EMPTY links list, NO parent (top-level)
    "REB-431",  # story A: comments + 3 links + parent (REB-430)
]

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "jira"

# Keep only recent comments to bound fixture size, but preserve each ADF
# body/author/id and the nested comment shape consumed by the differ.
_MAX_COMMENTS_PER_ISSUE = 2

# Scrub structured secret fields, then regex-scrub strings for emails and
# raw or URL-encoded account IDs. Preserve JSON keys and value types so the
# fixture remains a faithful response shape.
_REDACTED_EMAIL = "redacted@example.invalid"
_REDACTED_ACCOUNTID = "redacted:00000000-0000-0000-0000-000000000000"
_REDACTED_NAME = "Redacted User"

# key name -> replacement value (string shape preserved)
_KEY_REPLACEMENTS = {
    "emailAddress": _REDACTED_EMAIL,
    "accountId": _REDACTED_ACCOUNTID,
    "displayName": _REDACTED_NAME,
}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Jira Cloud accountId: "<digits>:<uuid>" raw, or "%3A"-encoded inside URLs. The
# digit-prefix length varies across Atlassian account vintages, so match >=3.
_ACCOUNTID_RE = re.compile(
    r"\d{3,}(?::|%3A)[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# Org-identifying Jira tenant host (``<tenant>.atlassian.net``). The generic
# internal cluster hosts (``*.prod.atl-paas.net``) are NOT org-identifying and are
# left intact so the fixtures stay shape-honest.
_TENANT_HOST_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.atlassian\.net")


def _scrub_string(value: str) -> str:
    value = _ACCOUNTID_RE.sub("REDACTED-ACCOUNTID", value)
    value = _EMAIL_RE.sub(_REDACTED_EMAIL, value)
    value = _TENANT_HOST_RE.sub("https://example.atlassian.net", value)
    return value


def scrub(obj: Any, _key: str | None = None) -> Any:
    """Recursively redact secrets/PII while preserving JSON structure."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in _KEY_REPLACEMENTS and isinstance(v, str):
                out[k] = _KEY_REPLACEMENTS[k]
            else:
                out[k] = scrub(v, _key=k)
        return out
    if isinstance(obj, list):
        return [scrub(v, _key=_key) for v in obj]
    if isinstance(obj, str):
        return _scrub_string(obj)
    return obj


def _trim_comments(comment_map: dict[str, Any], cap: int) -> dict[str, Any]:
    """Keep the most recent ``cap`` comments while preserving Jira's other comment-field keys
    and each retained comment's shape."""
    trimmed: dict[str, Any] = {}
    for key, field in comment_map.items():
        if isinstance(field, dict) and isinstance(field.get("comments"), list):
            field = {**field, "comments": field["comments"][-cap:]}
        trimmed[key] = field
    return trimmed


def _write(name: str, payload: Any) -> Path:
    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = _FIXTURE_DIR / name
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    return path


def capture(keys: list[str]) -> None:
    # Import the production transport lazily so --help works without the package.
    from rebar.reconciler.adapters.jira import acli as acli_mod
    from rebar.reconciler.adapters.jira import acli_subprocess

    settings = acli_subprocess.resolve_jira_settings(project_default="REB")
    project = settings.project or "REB"
    jql = f"key in ({','.join(keys)}) ORDER BY key ASC"
    print(f"capture: project={project} jql={jql!r}", file=sys.stderr)

    client = acli_mod.AcliClient(
        jira_url=settings.url, user=settings.user, api_token=settings.api_token
    )

    # Drive the SAME production methods the fetcher calls — no parallel path.
    search = client.search_issues(jql)
    comment_map = client.get_comment_map(project, jql)
    issuelinks_map = client.get_issuelinks_map(project, jql)
    parent_map = client.get_parent_map(project, jql)

    comment_map = _trim_comments(comment_map, _MAX_COMMENTS_PER_ISSUE)

    meta = {
        "_comment": (
            "Scrubbed real Jira REST payloads captured via the production "
            "AcliClient methods. Regenerate with scripts/capture_jira_fixtures.py "
            "(REBAR_CAPTURE_JIRA_FIXTURES=1). DO NOT hand-edit — fixtures must stay "
            "an honest mirror of real Jira shapes (see docs/jira-fixtures.md)."
        ),
        "project": project,
        "jql": jql,
        "keys": keys,
        "scrubbed": True,
    }

    _write("search.json", scrub(search))
    _write("comment_map.json", scrub(comment_map))
    _write("issuelinks_map.json", scrub(issuelinks_map))
    _write("parent_map.json", scrub(parent_map))
    _write("_meta.json", meta)
    print(
        f"capture: wrote {len(search)} search issues, "
        f"{len(comment_map)} comment / {len(issuelinks_map)} issuelinks / "
        f"{len(parent_map)} parent entries to {_FIXTURE_DIR}",
        file=sys.stderr,
    )


def main(argv: list[str]) -> int:
    if not _truthy(os.environ.get("REBAR_CAPTURE_JIRA_FIXTURES")):
        print(
            "capture_jira_fixtures: LIVE capture is opt-in. Re-run with\n"
            "  REBAR_CAPTURE_JIRA_FIXTURES=1 JIRA_PROJECT=REB "
            "python scripts/capture_jira_fixtures.py\n"
            "It reads real Jira (read-only) and writes scrubbed fixtures to "
            "tests/fixtures/jira/.",
            file=sys.stderr,
        )
        return 2
    keys = argv[1:] or DEFAULT_KEYS
    capture(keys)
    return 0


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
