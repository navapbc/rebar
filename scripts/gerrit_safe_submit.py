#!/usr/bin/env python3
"""Submit a Gerrit change only if the inspected revision is still current."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://rebar.solutions.navateam.com"
SESSION_ENV = ("REBAR_SESSION_ID", "COPILOT_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "SESSION_ID")


def _credential(host: str) -> tuple[str, str]:
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input=f"protocol=https\nhost={host}\n\n",
        text=True,
        capture_output=True,
        check=True,
    )
    fields = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    user = fields.get("username")
    password = fields.get("password")
    if not user or not password:
        raise RuntimeError(f"git credential helper returned no HTTPS credential for {host}")
    return user, password


def _strip_xssi(text: str) -> str:
    text = text.lstrip()
    return text[4:].strip() if text.startswith(")]}'") else text.strip()


def _request(base_url: str, method: str, path: str, auth: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": auth, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base_url}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(_strip_xssi(raw) or "{}")


def _label_ok(change: dict, label: str) -> bool:
    info = (change.get("labels") or {}).get(label) or {}
    return bool(info.get("approved"))


def _revision_matches(current: str | None, inspected: str) -> bool:
    return bool(current) and len(inspected) == 40 and current == inspected


def _session_hashtag() -> str | None:
    for name in SESSION_ENV:
        value = os.environ.get(name, "").strip()
        if value:
            safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
            return f"rebar-session-{safe[:48]}" if safe else None
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("change", help="Gerrit numeric change id or Change-Id")
    parser.add_argument(
        "--revision",
        required=True,
        help="full 40-character commit SHA whose live votes you inspected",
    )
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--hashtag", default=_session_hashtag())
    args = parser.parse_args(argv)

    host = urllib.parse.urlparse(args.base_url).hostname or "rebar.solutions.navateam.com"
    user, password = _credential(host)
    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    change = urllib.parse.quote(args.change, safe="")

    detail = _request(
        args.base_url.rstrip("/"),
        "GET",
        f"/a/changes/{change}?o=CURRENT_REVISION&o=DETAILED_LABELS&o=SUBMITTABLE",
        auth,
    )
    current = detail.get("current_revision")
    if not _revision_matches(current, args.revision):
        print(
            f"refusing stale submit: inspected {args.revision}, current is {current}",
            file=sys.stderr,
        )
        return 2
    missing = [label for label in ("LLM-Review", "Verified") if not _label_ok(detail, label)]
    unresolved = detail.get("unresolved_comment_count", 0)
    if missing or unresolved or not detail.get("submittable", False):
        print(
            "refusing submit: "
            f"missing live labels={missing}, unresolved={unresolved}, "
            f"submittable={detail.get('submittable', False)}",
            file=sys.stderr,
        )
        return 3

    if args.hashtag:
        _request(
            args.base_url.rstrip("/"),
            "POST",
            f"/a/changes/{change}/hashtags",
            auth,
            {"add": [args.hashtag]},
        )

    try:
        _request(
            args.base_url.rstrip("/"),
            "POST",
            f"/a/changes/{change}/revisions/{current}/submit",
            auth,
            {},
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            print(
                "refusing stale submit: Gerrit reports revision is no longer current",
                file=sys.stderr,
            )
            return 2
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
