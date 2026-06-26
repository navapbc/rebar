#!/usr/bin/env python3
"""Capture + scrub real Jira REST payloads into hermetic test fixtures.

Story A (fe57-7712-1e3f-45f4) of epic f89d: the escaped Jira-sync bugs all began
with unit tests feeding the differs hand-built ``jira_snapshot`` dicts in shapes
the real fetcher never produces (flat ``comments`` instead of nested ``comment``;
``issuelinks`` the snapshot never carried). Per ``jira-cli``'s pattern, the fix is
**fixtures captured from real Jira payloads, served through the production
serialization path** — never hand-massaged. This script is the live-gated capture
tool that keeps ``tests/fixtures/jira/*.json`` honest.

What it captures (the exact four responses ``fetcher._build_snapshot`` consumes,
through the PRODUCTION ``AcliClient`` methods — no parallel serialization):

  * ``search.json``         — ``AcliClient.search_issues`` (the base issue list)
  * ``comment_map.json``    — ``AcliClient.get_comment_map``    ({key: {"comments": [...]}})
  * ``issuelinks_map.json`` — ``AcliClient.get_issuelinks_map`` ({key: [issuelink, ...]})
  * ``parent_map.json``     — ``AcliClient.get_parent_map``     ({key: parent_key | None})

Secret-scrubbing (layered, vcrpy-style redaction) runs on every captured payload so
the committed fixtures carry NO tokens/emails/accountIds. ``FakeAcliClient``
(tests/integration/rebar_reconciler/jira_contract/_fakes.py) replays these verbatim through the
production fetcher; ``jira_contract/test_jira_fixtures.py`` independently re-asserts
the scrub held.

Usage (LIVE — opt-in, reads real Jira, never writes to Jira):

    REBAR_CAPTURE_JIRA_FIXTURES=1 JIRA_PROJECT=REB \\
        python scripts/capture_jira_fixtures.py

Re-capture cadence: refresh whenever the Jira REST shape the fetcher consumes may
have changed (a Jira Cloud API change, or a new enrichment field added to
``_build_snapshot``). The capture is read-only; commit the regenerated fixtures and
let ``test_snapshot_contract.py`` (story B) + ``test_jira_fixtures.py`` gate
the refresh. See docs/jira-fixtures.md.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# The curated capture set: a small, stable neighbourhood of REB issues that
# jointly exercises every enrichment the fetcher merges — nested comments,
# issue-links (incl. an authoritative EMPTY list on the epic), parent links
# (incl. a top-level null parent), and a non-null assignee. Kept small so the
# committed fixtures stay reviewable; refresh via this script, never by hand.
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

# Keep fixtures lean: retain at most this many (most-recent) comments per issue.
# Each retained comment keeps its FULL real shape (ADF body, author dict, ids) —
# only the COUNT is trimmed, so the producer/consumer contract the differ reads
# (nested ``comment`` -> ``comments`` list of author/body dicts) is exercised
# without committing hundreds of KB of verbose real comment bodies.
_MAX_COMMENTS_PER_ISSUE = 2

# ---------------------------------------------------------------------------
# Layered secret-scrubbing.
#
# Two layers, applied to every captured payload:
#   (1) key-based value replacement for the structured secret/PII fields, and
#   (2) a regex sweep over every remaining string for the concrete leakage
#       vectors (emails, raw + url-encoded Jira accountIds) that hide inside
#       free-form URL/self/avatar values.
# Both preserve the JSON SHAPE (keys + value types) so the fixtures stay an
# honest mirror of the real REST response — only the secret *values* change.
# ---------------------------------------------------------------------------

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
    """Retain at most ``cap`` (most-recent) comments per issue; keep shape.

    The Jira ``comment`` field is ``{"comments": [...], "total": N, ...}``. We
    slice the ``comments`` list to its last ``cap`` entries (most recent) but
    leave every other key (``total``, ``startAt``, ``maxResults``, ``self``)
    exactly as Jira reported, so the field stays shape-honest.
    """
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
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "rebar" / "_engine"))
    from rebar_reconciler import acli as acli_mod  # noqa: PLC0415
    from rebar_reconciler import acli_subprocess  # noqa: PLC0415

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
    return bool(value) and value.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
