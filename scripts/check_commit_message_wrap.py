#!/usr/bin/env python3
"""Commit-message body wrap checker — the 50/72 rule, enforced at commit time.

Gerrit renders a change description as preformatted text and preserves the author's
newlines verbatim, so the wrapping you commit is the wrapping reviewers read (and what
``git log`` shows in an 80-column terminal). Gerrit's bundled
``commit-message-length-validator`` only *warns* on a long subject or body line, which
is easy to ignore — this hook makes the same rule a local commit gate.

The convention matches every major Gerrit-based project: Go ("wrapped at around 72
columns"), Chromium ("wrapped to 72 columns for easier log message viewing in
terminals"), ChromiumOS ("wrap the body text to 72 characters so that ``git log``
looks nice") and Android ("hard-wraps at 72 characters maximum").

Deliberate carve-outs — things that must NOT be wrapped, because wrapping them breaks
machine parsing or makes them unusable:

* **trailers** (``rebar-ticket:``, ``Change-Id:``, ``Signed-off-by:``, ``Fixes:`` …)
* **fenced code blocks** (``` ``` ```) and indented blocks, which are verbatim
* **long unbreakable tokens** — a URL, path, or hash that simply cannot be split
* **table-ish / comment lines** (``|`` rows, and everything git strips: ``#`` lines)

Usage (pre-commit passes the message file; CLI use is for testing):

    check_commit_message_wrap.py .git/COMMIT_EDITMSG
    check_commit_message_wrap.py --subject-limit 50 --body-limit 72 <file>
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SUBJECT_LIMIT = 50
BODY_LIMIT = 72

# A trailer is ``Token-Or-Word:`` followed by a value — git's own interpret-trailers
# shape, plus this repo's lowercase ``rebar-ticket:``. Never wrapped: the value is
# parsed by CI, Gerrit, and the DCO gate.
_TRAILER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:\s")

# ``BUG=``/``TEST=``-style tags (the Chromium/ChromiumOS convention) are also atomic.
_TAG_RE = re.compile(r"^[A-Z][A-Z0-9_]*=")

_FENCE_RE = re.compile(r"^\s*```")

# An "atomic" token has structure that implies it is a single unsplittable thing —
# a scheme (https://), a path separator, or a long run of dotted/underscored/hyphenated
# identifier segments. Plain repeated letters do not qualify: that is unwrapped prose.
# (A bare hex run is deliberately NOT listed: a git sha is at most 64 chars, so it can
# never by itself exceed a 72-column limit, and treating long hex-ish runs as atomic
# would silently excuse unwrapped prose made of a/b/c/d/e/f characters.)
_ATOMIC_TOKEN_RE = re.compile(r"://|[/\\]|\w[._-]\w")

# A line is unwrappable only when a SINGLE token already exceeds the limit *and* the
# line is essentially just that token (plus short lead-in) — a URL, module path, or git
# sha the author physically cannot split. A long line made of many short words IS
# wrappable, and so is one long token followed by lots of prose that should have been
# moved to the next line, so neither is exempt.
def _has_unbreakable_token(line: str, limit: int) -> bool:
    over = [t for t in line.split() if len(t) > limit]
    if not over:
        return False
    # Only ATOMIC tokens earn the exemption: a URL, a filesystem/module path, a git
    # sha, a long dotted/underscored identifier — things with no natural break point.
    # A same-length run of ordinary characters is just unwrapped prose, so it is still
    # reported. And whatever precedes the atomic token must itself fit on a line.
    if not all(_ATOMIC_TOKEN_RE.search(t) for t in over):
        return False
    remainder = len(line.strip()) - max(len(t) for t in over)
    return remainder <= limit


def _is_exempt(line: str, *, in_fence: bool) -> bool:
    """Is *line* legitimately allowed to exceed the body limit?"""
    if in_fence:
        return True  # verbatim block — never reflow
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith("#"):
        return True  # git strips comment lines from the message entirely
    if line.startswith(("    ", "\t")):
        return True  # indented literal block (code / output / diff)
    if stripped.startswith("|"):
        return True  # table row
    if _TRAILER_RE.match(stripped) or _TAG_RE.match(stripped):
        return True
    return _has_unbreakable_token(stripped, BODY_LIMIT)


def _strip_comments_and_diff(raw: str) -> list[str]:
    """Drop what git itself removes: comment lines and the ``--verbose`` diff."""
    out: list[str] = []
    for line in raw.split("\n"):
        # `git commit --verbose` appends a diff below this marker; it is not part of
        # the message and its lines are routinely > 72 chars.
        if line.startswith("# ------------------------ >8 ------------------------"):
            break
        out.append(line)
    return out


def check_message(raw: str, *, subject_limit: int, body_limit: int) -> list[str]:
    """Return a list of human-readable problems (empty == the message conforms)."""
    lines = _strip_comments_and_diff(raw)
    content = [ln for ln in lines if not ln.strip().startswith("#")]
    while content and not content[0].strip():
        content.pop(0)
    if not content:
        return []  # empty message: git aborts the commit itself

    problems: list[str] = []

    subject = content[0]
    if len(subject) > subject_limit and not _has_unbreakable_token(subject, subject_limit):
        problems.append(
            f"subject is {len(subject)} chars (limit {subject_limit}): {subject[:60]!r}…"
        )

    # A blank line must separate subject from body — git uses it as the heuristic for
    # `git log --oneline`, `%s`/`%b`, and Gerrit's subject extraction.
    if len(content) > 1 and content[1].strip():
        problems.append("no blank line after the subject line")

    in_fence = False
    for offset, line in enumerate(content[1:], start=2):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if len(line) > body_limit and not _is_exempt(line, in_fence=in_fence):
            problems.append(f"line {offset} is {len(line)} chars (limit {body_limit}): {line[:56]!r}…")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message_file", type=Path, help="path to the commit message file")
    parser.add_argument("--subject-limit", type=int, default=SUBJECT_LIMIT)
    parser.add_argument("--body-limit", type=int, default=BODY_LIMIT)
    args = parser.parse_args(argv)

    try:
        raw = args.message_file.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"commit-message wrap: cannot read {args.message_file}: {exc}", file=sys.stderr)
        return 1

    problems = check_message(
        raw, subject_limit=args.subject_limit, body_limit=args.body_limit
    )
    if not problems:
        return 0

    print("commit message does not follow the 50/72 rule:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print(
        "\nGerrit renders the change description as preformatted text, so these\n"
        f"newlines are what reviewers see. Wrap the body at {args.body_limit} columns\n"
        f"and keep the subject under {args.subject_limit}. Trailers, URLs, table rows\n"
        "and fenced code blocks are exempt. See CONTRIBUTING.md §2a.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
