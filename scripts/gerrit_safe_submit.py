#!/usr/bin/env python3
"""Submit a Gerrit change only if the inspected revision is still current.

Exit codes:
  0  submitted, or the change is already merged with --revision among its patch sets
     (says so; nothing is sent).
  2  refused: --revision is not a full 40-character commit SHA (checked before any
     Gerrit request), or the revision is stale (checked before submitting, or shown by
     re-reading the change after Gerrit answered 409), or Gerrit refused the submit with 409 for
     another reason (merge conflict, unmet submit requirement). stderr says which and
     prints Gerrit's response.
  3  refused before submitting: the change is abandoned, a required vote is missing,
     comments are unresolved,
     or Gerrit reports the change not submittable.
  4  no Gerrit credential for the host.
  1  the submit failed (any other HTTP error, a timeout or connection error, or an
     unreadable response) and a re-read shows the change is not merged at the revision;
     stderr prints the error and Gerrit's response. When such an error comes back but
     Gerrit did merge the revision, the script says so and exits 0.
  A failure to add the session hashtag only warns; the submit still runs.
"""

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
from collections.abc import Callable

from rebar._store.gitutil import credential_git_env

BASE_URL = "https://rebar.solutions.navateam.com"
SESSION_ENV = ("REBAR_SESSION_ID", "COPILOT_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "SESSION_ID")


def _credential(host: str) -> tuple[str, str]:
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input=f"protocol=https\nhost={host}\n\n",
            text=True,
            capture_output=True,
            check=True,
            env={**credential_git_env(os.environ), "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(f"git credential fill failed for {host}: {detail}") from None
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


def _error_body(exc: urllib.error.HTTPError) -> str:
    """Return the reason Gerrit gave for a failed request, for the operator to read.

    A JSON object with a string ``message`` yields that message; any other body (plain
    text, other JSON) is returned as-is after the ``)]}'`` prefix is stripped.
    """
    try:
        raw = exc.read()
    except OSError:
        return ""
    text = _strip_xssi(raw.decode("utf-8", "replace"))
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    if isinstance(parsed, dict) and isinstance(parsed.get("message"), str):
        return parsed["message"]
    return text


def _explain_409(body: str, submitted: str | None, reread: Callable[[], dict]) -> str:
    """Name what a 409 on submit means, claiming staleness only when a re-read shows it."""
    try:
        now = reread().get("current_revision")
    except (urllib.error.URLError, OSError):
        now = None
    if now and now != submitted:
        return (
            "refusing stale submit: Gerrit reports revision is no longer current: "
            f"submitted {submitted}, current is {now}"
        )
    if "conflict" in body.lower():
        return (
            "refusing submit: Gerrit cannot merge this change (merge conflict); rebase it "
            "onto origin/main, resolve the conflict, and push a new patch set"
        )
    return "refusing submit: Gerrit rejected the submit (HTTP 409)"


def _label_ok(change: dict, label: str) -> bool:
    info = (change.get("labels") or {}).get(label) or {}
    return bool(info.get("approved"))


def _is_full_sha(revision: str) -> bool:
    return len(revision) == 40 and all(ch in "0123456789abcdefABCDEF" for ch in revision)


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
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("change", help="Gerrit numeric change id or Change-Id")
    parser.add_argument(
        "--revision",
        required=True,
        help="full 40-character commit SHA whose live votes you inspected",
    )
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--hashtag", default=_session_hashtag())
    args = parser.parse_args(argv)
    if not _is_full_sha(args.revision):
        print(
            f"refusing submit: --revision {args.revision!r} is not a full 40-character "
            "commit SHA; pass the full SHA of the patch set whose votes you inspected "
            "(e.g. `git rev-parse HEAD`)",
            file=sys.stderr,
        )
        return 2

    host = urllib.parse.urlparse(args.base_url).hostname or "rebar.solutions.navateam.com"
    try:
        user, password = _credential(host)
    except RuntimeError as exc:
        print(f"missing Gerrit credential for {host}: {exc}", file=sys.stderr)
        return 4
    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    change = urllib.parse.quote(args.change, safe="")

    def read_change() -> dict:
        return _request(
            args.base_url.rstrip("/"),
            "GET",
            f"/a/changes/{change}?o=CURRENT_REVISION&o=DETAILED_LABELS&o=SUBMITTABLE",
            auth,
        )

    detail = read_change()
    current = detail.get("current_revision")
    closed = _closed_change_exit(
        args.change, args.revision, detail, _read_all_revisions(args, change, auth)
    )
    if closed is not None:
        return closed
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
        _add_hashtag(args.base_url.rstrip("/"), change, auth, args.hashtag)

    try:
        _request(
            args.base_url.rstrip("/"),
            "POST",
            f"/a/changes/{change}/revisions/{current}/submit",
            auth,
            {},
        )
    except urllib.error.HTTPError as exc:
        body = _error_body(exc)
        reason = f"Gerrit response (HTTP {exc.code}): {body}"
        if exc.code == 409:
            print(_explain_409(body, current, read_change), file=sys.stderr)
            print(reason, file=sys.stderr)
            return 2
        return _reconcile(
            args.change, args.revision, reason, _read_all_revisions(args, change, auth)
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = f"submit request failed: {exc!r}"
        return _reconcile(
            args.change, args.revision, reason, _read_all_revisions(args, change, auth)
        )
    return 0


def _closed_change_exit(
    change: str, revision: str, detail: dict, reread: Callable[[], dict]
) -> int | None:
    """Report a change that is already closed, before the stale and vote checks; ``None``
    when it is open.

    Gerrit omits the labels' ``approved`` summary on a closed change, so the vote check would
    report votes that exist as missing (bug froggy-brightish-xiphias). A merge that includes
    ``revision`` among its patch sets (Rebase-If-Necessary may merge a newer one) is success,
    as on the reconcile path. Nothing is POSTed on any of these paths.
    """
    status = detail.get("status")
    current = detail.get("current_revision")
    if status == "ABANDONED":
        print(f"refusing submit: change {change} is abandoned", file=sys.stderr)
        return 3
    if status != "MERGED":
        return None
    revisions = detail.get("revisions") or {}
    if revision not in revisions:
        revisions = reread().get("revisions") or {}
    if revision in revisions:
        print(
            f"change {change} is already merged at {current} "
            f"(includes {revision}); nothing to submit"
        )
        return 0
    print(
        f"refusing submit: change {change} is MERGED at {current}, "
        f"not at the inspected revision {revision}; nothing submitted",
        file=sys.stderr,
    )
    return 2


def _add_hashtag(base_url: str, change: str, auth: str, hashtag: str) -> None:
    """Tag the change with the session hashtag; a failure only warns (ownership metadata
    must never block or misreport a submit)."""
    try:
        _request(base_url, "POST", f"/a/changes/{change}/hashtags", auth, {"add": [hashtag]})
    except urllib.error.HTTPError as exc:
        print(
            f"warning: could not add hashtag {hashtag} (HTTP {exc.code}): {_error_body(exc)}",
            file=sys.stderr,
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"warning: could not add hashtag {hashtag}: {exc!r}", file=sys.stderr)


def _read_all_revisions(args: argparse.Namespace, change: str, auth: str) -> Callable[[], dict]:
    def read() -> dict:
        return _request(
            args.base_url.rstrip("/"), "GET", f"/a/changes/{change}?o=ALL_REVISIONS", auth
        )

    return read


def _reconcile(change: str, revision: str, reason: str, reread: Callable[[], dict]) -> int:
    """Decide a submit whose request raised: Gerrit may have merged before the error.

    Exit 0 when a re-read shows the change MERGED with ``revision`` among its patch sets
    (Rebase-If-Necessary adds a rebased patch set, so the merged one may be newer).
    Otherwise print the original error and exit 1.
    """
    try:
        detail = reread()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(reason, file=sys.stderr)
        print(f"could not re-read change {change} to check for a merge: {exc!r}", file=sys.stderr)
        return 1
    if detail.get("status") == "MERGED" and revision in (detail.get("revisions") or {}):
        print(
            f"submitted: change {change} merged (revision {revision}); "
            f"the submit request itself reported: {reason}"
        )
        return 0
    print(reason, file=sys.stderr)
    print(f"change {change} is not merged (status {detail.get('status')})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
