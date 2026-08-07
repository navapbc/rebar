"""The write-time secret screen: refuse event bodies carrying live credentials (bug e7a9).

On 2026-08-03 a session posted a comment containing a full environment dump with seven
live credentials. GitHub push protection rejected ``refs/heads/tickets`` (GH013), so
EVERY session's store writes queued local-only — a store-sharing outage caused by one
comment, stopped only by a control outside this project. This module is that control,
inside it.

Design, per the operator decision recorded on e7a9 (2026-08-07) — **REFUSE, with an
allowed force override**:

* **Refuse, never redact.** Silently rewriting a caller's payload makes the stored event
  differ from what the caller believes it wrote, and in an append-only event store that
  divergence is unrecoverable. Refusing is loud, reversible, and leaves the caller in
  control.
* **Report the family and the location, never the value.** A finding carries the matched
  family, the field path, and the line number — and nothing derived from the secret
  itself, not even a truncated prefix. A guard that leaks the secret while reporting it
  is worse than no guard, so :class:`SecretFinding` structurally cannot hold one.
* **Live shapes only.** The store's own corpus says DESCRIPTIONS of credentials are the
  common case: sweeping 3,299 ticket dirs for ``sk-ant-api03`` found five matches and
  none was a credential — two were e7a9's own CREATE and EDIT events. So each pattern
  demands the full live length, and the high-entropy families additionally demand a
  Shannon-entropy floor, which separates a real key from ``sk-ant-api03-...`` (truncated)
  and ``sk-ant-api03-[A-Za-z0-9_-]{93,}`` (a regex literal) without having to infer
  intent. Measured: sweeping the live store's 25,125 events with the table below refuses
  exactly ONE, and that one is ``AKIAIOSFODNN7EXAMPLE`` — AWS's own published doc example,
  quoted in a ticket about secret scanning, and recoverable via the override. Zero hits
  across this repo's docs and source. That is what makes the screen safe on by default.

:func:`scan_text` is a pure ``str -> findings`` function with no I/O, so it is reusable
outside the store seam; :mod:`rebar.llm.workflow.lint` consumes :data:`SECRET_PATTERNS`
for its workflow-file sweep rather than keeping a second, drifting table.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

__all__ = [
    "SECRET_PATTERNS",
    "SecretFinding",
    "iter_line_matches",
    "override_record",
    "refusal_message",
    "scan_text",
    "screen_event_data",
]

# Shannon entropy (bits/char) a matched body must clear for the high-entropy families.
# A real 95-char base62 key sits near 5.9; the benign look-alikes in the store's corpus
# are either too short to match at all or are low-entropy prose fragments.
_MIN_ENTROPY = 3.0

# ``(family, pattern, entropy_checked)``. Every pattern demands the LIVE length — a
# truncated placeholder or a bracket-expression regex literal cannot reach it.
#
# The entropy-checked families match a FIXED-WIDTH window (``{n}``, no upper-open
# quantifier and no trailing boundary) rather than a greedy run. That is deliberate and
# security-relevant: with a greedy ``{80,}`` plus a trailing lookahead, appending
# same-character-class padding after a real key — ``<key>`` + 400 ``-`` characters, which
# is what an ASCII rule in a log or an env dump looks like — both dragged the whole-match
# entropy under the floor AND pushed the boundary out, so the screen FAILED OPEN on a
# genuine credential. A fixed window is measured on the key's own bytes, so nothing
# appended after it can dilute the entropy or move the match.
#
# Families where the shape alone is decisive (fixed total length, or a literal header)
# skip the entropy check and keep a trailing negative lookahead — a lookahead rather than
# ``\b`` because several charsets end in ``-``/``_``, where ``\b`` misbehaves.
_TOKEN_TAIL = r"(?![A-Za-z0-9_-])"

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("Anthropic API key", re.compile(r"\bsk-ant-[a-z0-9]{2,12}-[A-Za-z0-9_-]{80}"), True),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{40}"), True),
    ("OpenAI legacy API key", re.compile(r"\bsk-[A-Za-z0-9]{48}"), True),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}"), True),
    ("GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60}"), True),
    ("Google API key", re.compile(rf"\bAIza[0-9A-Za-z_-]{{35}}{_TOKEN_TAIL}"), True),
    ("Atlassian API token", re.compile(r"\bATATT[A-Za-z0-9_\-=]{100}"), True),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20}"), False),
    ("AWS access key id", re.compile(rf"\bAKIA[0-9A-Z]{{16}}{_TOKEN_TAIL}"), False),
    (
        "AWS secret access key",
        re.compile(r"(?i)\baws_secret_access_key\b\W{0,4}[A-Za-z0-9/+]{40}"),
        False,
    ),
    ("PyPI API token", re.compile(r"\bpypi-AgE[A-Za-z0-9_-]{50}"), True),
    ("Stripe live key", re.compile(r"\bsk_live_[A-Za-z0-9]{24}"), True),
    # Anchored to a whole line: a real PEM block puts its banner on its own line, whereas
    # prose ENUMERATING the banners (a backticked `BEGIN RSA PRIVATE KEY`, which is what a
    # ticket about secret scanning looks like) always has surrounding text. Sweeping the
    # 25,125-event live store, this anchor is the difference between refusing that ticket
    # and not. (This comment abbreviates the banner instead of quoting it in full: the
    # repo-wide scanner cannot tell a documented banner from a real one and flagged the
    # full form here — the very false positive this pattern is tuned against. Abbreviating
    # is preferred over a scanner allowlist, which would disarm the detector on the file
    # that most needs it armed.)
    (
        "private key block",
        re.compile(r"^\s*-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----\s*$"),
        False,
    ),
    (
        "URL with an inline password",
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s:/@]{8,}@[^\s/@]+"),
        False,
    ),
    (
        "Slack webhook URL",
        re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{40}"),
        False,
    ),
)


@dataclass(frozen=True)
class SecretFinding:
    """One detection: WHICH family fired and WHERE — never the value.

    The absence of a value field is the point, not an omission: the refusal path is
    reported to stderr and logs, so a finding that carried the secret (or a prefix of
    it) would turn the guard into a second leak channel.
    """

    family: str
    field: str
    line: int

    def describe(self) -> str:
        return f"{self.family} at {self.field} line {self.line}"


def _entropy(value: str) -> float:
    """Shannon entropy in bits/char (0.0 for the empty string)."""
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def iter_line_matches(line: str) -> Iterator[tuple[str, re.Match[str]]]:
    """Yield ``(family, match)`` for each family matching *line*, at most once each.

    The shared primitive: the store's refusal path and
    :mod:`rebar.llm.workflow.lint`'s workflow-file sweep both drive off this, so the
    pattern table and its entropy floor have exactly one definition to keep current.
    """
    for family, pattern, entropy_checked in SECRET_PATTERNS:
        for match in pattern.finditer(line):
            if entropy_checked and _entropy(match.group(0)) < _MIN_ENTROPY:
                continue
            yield family, match
            break  # one hit per family per line is enough


def scan_text(text: str, *, field: str = "body") -> list[SecretFinding]:
    """Findings for every live-shaped credential in *text*, located by line.

    Pure: no I/O, no logging, no exceptions. Line numbers are 1-based within *text*
    so a caller can point at the offending line without reproducing it.
    """
    return [
        SecretFinding(family, field, lineno)
        for lineno, line in enumerate(text.splitlines() or [text], start=1)
        for family, _match in iter_line_matches(line)
    ]


_MAX_KEY_SEGMENT = 32


def _safe_segment(key: object) -> str:
    """A dict key rendered safe to print in a refusal.

    The field path is printed to stderr and logs, so a key is a leak channel just like a
    value: an event whose payload key IS a credential (``{"<key>": ...}``) would
    otherwise be echoed verbatim by the refusal that was meant to contain it. Any key
    that itself matches a credential shape is replaced, and every key is length-capped so
    a long opaque blob cannot ride along in the path.
    """
    text = str(key)
    if next(iter_line_matches(text), None) is not None:
        return "<redacted-key>"
    return text if len(text) <= _MAX_KEY_SEGMENT else f"{text[:_MAX_KEY_SEGMENT]}…"


def screen_event_data(data: object, *, prefix: str = "data") -> list[SecretFinding]:
    """Findings across every string reachable in an event's ``data`` payload.

    Walks dicts and lists so free text is screened wherever it lives — ``body`` on a
    COMMENT, ``title``/``description`` on a CREATE, ``fields.description`` on an EDIT —
    without this module needing to know each writer's shape. Dict KEYS are screened too:
    an imported or library-supplied payload can put a credential in a key just as easily
    as in a value.
    """
    findings: list[SecretFinding] = []
    if isinstance(data, str):
        findings.extend(scan_text(data, field=prefix))
    elif isinstance(data, dict):
        for key, value in data.items():
            segment = _safe_segment(key)
            if segment == "<redacted-key>":
                findings.append(SecretFinding("credential-shaped payload key", prefix, 1))
            findings.extend(screen_event_data(value, prefix=f"{prefix}.{segment}"))
    elif isinstance(data, (list, tuple)):
        for index, value in enumerate(data):
            findings.extend(screen_event_data(value, prefix=f"{prefix}[{index}]"))
    return findings


def override_record(findings: Iterable[SecretFinding], reason: str) -> dict[str, object]:
    """The audit stamp a forced write carries on its event: WHY, and what was bypassed.

    Carries no material derived from the secret — only the operator's reason, the family
    names, and the field/line locations. WHO comes from the event envelope's ``author``
    and attribution fields, which every write path stamps.
    """
    materialised = list(findings)
    return {
        "reason": reason,
        "families": sorted({f.family for f in materialised}),
        "locations": sorted({f"{f.field}:{f.line}" for f in materialised}),
    }


def refusal_message(findings: Iterable[SecretFinding], *, override_flag: str) -> str:
    """The teaching refusal: what matched, where, why, and how to proceed anyway.

    Contains no material derived from the secret — only family names, field paths, and
    line numbers, all of which come from :class:`SecretFinding`.
    """
    located = "; ".join(f.describe() for f in findings)
    return (
        f"Error: refusing to write — a live credential shape was detected ({located}). "
        "rebar's store auto-pushes, so a secret here can wedge the shared tickets branch "
        "behind push protection and block every session's writes. Remove the value (pass "
        "untrusted text via a file or a quoted heredoc — an unquoted shell command "
        "substitution is what caused the original incident). If this is a FALSE POSITIVE, "
        f"re-run with {override_flag}=<reason> to record an audited forced write — but "
        "forcing a genuine credential through reproduces that outage for every session."
    )
