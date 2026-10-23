"""Refuse live credential shapes before ticket events reach the shared store.

Never alter a caller's payload. Report only credential family and location, never
the value or its prefix; an audited force override remains available. Full-length
patterns, with entropy checks for selected families, spare common truncated and
regex examples; a full-shape documentation key can still match.
Pure scanning and the pattern table are shared with workflow lint.
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

# Require this entropy floor for high-entropy key families; short or repetitive
# examples cannot pass it.
_MIN_ENTROPY = 3.0

# Patterns require live lengths. Entropy-checked families use fixed-width windows
# so appended padding cannot dilute a real key's entropy. Shape-only families
# use a negative lookahead because ``\b`` mishandles trailing ``-`` or ``_``.
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
    # A PEM banner occupies a whole line; prose that names one does not.
    # Keep the example abbreviated so the repo scanner stays active here.
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
    """A credential family and location, without secret material for logs to leak."""

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
    """Yield at most one entropy-qualified match per family on a line.

    Store refusal and workflow lint share this pattern table.
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
    """Redact credential-shaped keys and cap path segments before logging them."""
    text = str(key)
    if next(iter_line_matches(text), None) is not None:
        return "<redacted-key>"
    return text if len(text) <= _MAX_KEY_SEGMENT else f"{text[:_MAX_KEY_SEGMENT]}…"


def screen_event_data(data: object, *, prefix: str = "data") -> list[SecretFinding]:
    """Scan nested event strings and dictionary keys without assuming event shape."""
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
    """Record the override reason, credential families, and locations only.

    Event attribution supplies the operator identity; no secret material is stored.
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
