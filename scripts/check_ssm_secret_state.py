#!/usr/bin/env python3
"""Reject SSM ``SecureString`` values that Terraform would persist in state.

The AWS provider refreshes a plaintext ``value`` into ``attributes.value`` even when
``ignore_changes = [value]`` prevents writes. ADR 0105 instead uses the write-only pair
``value_wo`` and ``value_wo_version``, which is never stored in state.

For each ``aws_ssm_parameter`` whose type is ``SecureString``, violations are:

* a literal, ``var.*``, or ``local.*`` ``value``;
* any ``insecure_value``;
* ``ignore_changes`` covering ``value``;
* only one member of the write-only pair; or
* no value source.

The complete write-only pair is allowed. A generated expression such as
``random_password.x.result`` is also allowed when ``ignore_changes`` does not cover it, because
its source already resides in state. Non-secret ``String``/``StringList`` parameters are ignored.

This hermetic static check reads HCL text without contacting AWS or exposing secret values.
Block matching accounts for braces inside strings, comments, and heredocs.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Directories never scanned (vendored / generated), mirroring the sibling terraform gate.
EXCLUDED_DIRS = frozenset({".venv", ".git", ".terraform", "node_modules"})

#: `resource "aws_ssm_parameter" "<name>" {` -- the opening line of a parameter block.
_RESOURCE_RE = re.compile(r'resource\s+"aws_ssm_parameter"\s+"([^"]+)"\s*\{')

#: `type = "SecureString"` inside a block body.
_TYPE_RE = re.compile(r'\btype\s*=\s*"([^"]+)"')

#: A bare `value =` (NOT `value_wo` / `value_wo_version`): `\bvalue\b` cannot match inside
#: `value_wo` because the following `_` is a word char, so there is no boundary after `value`.
#: Captures the whole right-hand side of the line so we can tell a quoted literal from an
#: expression, and a `var.`/`local.` reference from a terraform-generated one.
_VALUE_RE = re.compile(r"\bvalue\s*=\s*(.+)")

#: A `var.<x>` / `local.<x>` reference at the start of a value expression -- a real secret that
#: lands in state, NOT a terraform-generated value.
_VAR_LOCAL_RE = re.compile(r"(?:var|local)\.")

#: The write-only arguments.
_VALUE_WO_RE = re.compile(r"\bvalue_wo\s*=")
_VALUE_WO_VERSION_RE = re.compile(r"\bvalue_wo_version\s*=")

#: `insecure_value = ...` -- the provider's third value source (never valid for SecureString).
_INSECURE_VALUE_RE = re.compile(r"\binsecure_value\s*=")

#: `ignore_changes = [ ... value ... ]` -- capture the bracket body to test for `value`.
_IGNORE_CHANGES_RE = re.compile(r"ignore_changes\s*=\s*\[([^\]]*)\]")


@dataclass(frozen=True)
class Finding:
    """One SecureString parameter that persists (or would persist) a secret to state."""

    tf_file: str
    resource: str
    problem: str

    def render(self) -> str:
        return (
            f"{self.tf_file}: aws_ssm_parameter.{self.resource}: {self.problem} "
            "-- a SecureString secret must use write-only args (value_wo + value_wo_version), "
            "which are never persisted to terraform state (ADR 0105)."
        )


def _skip_string(text: str, j: int, n: int) -> int:
    """Given ``text[j] == '"'``, return the index just past the closing double quote.

    Backslash escapes are honoured. ``${...}`` interpolation is not descended into -- a nested
    unescaped ``"`` inside an interpolation is a rare shape not used by these parameter blocks.
    """
    k = j + 1
    while k < n:
        if text[k] == "\\":
            k += 2
            continue
        if text[k] == '"':
            return k + 1
        k += 1
    return n


def _skip_heredoc(text: str, j: int, n: int) -> int:
    """Given ``text[j:j+2] == '<<'``, skip an HCL heredoc; return the index past its terminator."""
    opener = re.match(r"<<-?\s*([A-Za-z_]\w*)", text[j:])
    if not opener:
        return j + 2  # a stray `<<` that is not a heredoc opener; treat as ordinary chars
    tag = opener.group(1)
    terminator = re.compile(rf"^[ \t]*{re.escape(tag)}[ \t]*$", re.MULTILINE)
    end = terminator.search(text, j)
    return end.end() if end else n


def _skip_noncode(text: str, j: int, n: int) -> int | None:
    """If ``text[j]`` starts a comment/string/heredoc, return the index past it; else ``None``."""
    ch = text[j]
    nxt = text[j + 1] if j + 1 < n else ""
    if ch == "#" or (ch == "/" and nxt == "/"):
        newline = text.find("\n", j)
        return n if newline == -1 else newline
    if ch == "/" and nxt == "*":
        end = text.find("*/", j + 2)
        return n if end == -1 else end + 2
    if ch == '"':
        return _skip_string(text, j, n)
    if ch == "<" and nxt == "<":
        return _skip_heredoc(text, j, n)
    return None


def _block_body(text: str, open_brace: int) -> str:
    """Return the ``{ ... }`` body starting at ``open_brace`` (the index of the ``{``).

    Brace matching is HCL-aware: a ``{`` or ``}`` inside a double-quoted string, a ``#``/``//`` line
    comment, a ``/* */`` block comment, or a ``<<``/``<<-`` heredoc does NOT change depth. Without
    this, a brace in a ``description`` or a JSON policy string would truncate the body and the gate
    could silently skip a SecureString block whose ``type``/``value`` sits past the stray brace.
    """
    depth, j, n = 1, open_brace + 1, len(text)
    while j < n and depth:
        skip = _skip_noncode(text, j, n)
        if skip is not None:
            j = skip
            continue
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
        j += 1
    return text[open_brace + 1 : j - 1]


def _ignores_value(body: str) -> bool:
    """True if any ``ignore_changes = [...]`` in the body lists ``value`` (not ``value_wo``)."""
    for match in _IGNORE_CHANGES_RE.finditer(body):
        items = {item.strip() for item in match.group(1).split(",")}
        if "value" in items:
            return True
    return False


def _securestring_problems(body: str) -> list[str]:
    """Problem strings for one ``SecureString`` block body (``[]`` == compliant)."""
    problems: list[str] = []
    value_match = _VALUE_RE.search(body)
    value_rhs = value_match.group(1).strip() if value_match else ""
    has_literal_value = value_rhs.startswith('"')
    has_expr_value = bool(value_rhs) and not has_literal_value
    has_insecure_value = bool(_INSECURE_VALUE_RE.search(body))
    has_value_wo = bool(_VALUE_WO_RE.search(body))
    has_value_wo_version = bool(_VALUE_WO_VERSION_RE.search(body))

    if has_literal_value:
        problems.append('persists a plaintext string-literal `value = "..."` into state')
    if has_expr_value and _VAR_LOCAL_RE.match(value_rhs):
        problems.append(
            "sources `value` from a `var.`/`local.` reference -- a real secret terraform reads "
            "into state, not a generated value"
        )
    if has_insecure_value:
        problems.append("uses `insecure_value`, which the provider stores to state as PLAINTEXT")
    if _ignores_value(body):
        problems.append("uses `ignore_changes = [value]`, the persisted-secret antipattern")
    if has_value_wo and not has_value_wo_version:
        problems.append("declares `value_wo` without the required `value_wo_version`")
    if has_value_wo_version and not has_value_wo:
        problems.append("declares `value_wo_version` without `value_wo`")
    if not (has_value_wo or has_expr_value or has_literal_value or has_insecure_value):
        problems.append("declares no value source (needs `value_wo` + `value_wo_version`)")
    return problems


def scan_text(tf_file: str, text: str) -> list[Finding]:
    """Findings for every ``aws_ssm_parameter`` SecureString block in one file's text."""
    findings: list[Finding] = []
    for res in _RESOURCE_RE.finditer(text):
        name = res.group(1)
        body = _block_body(text, text.index("{", res.start()))

        type_match = _TYPE_RE.search(body)
        if not type_match or type_match.group(1) != "SecureString":
            continue  # non-secret String/StringList params are out of scope

        findings.extend(Finding(tf_file, name, problem) for problem in _securestring_problems(body))
    return findings


def check_repo(root: Path) -> list[Finding]:
    """Every ``aws_ssm_parameter`` SecureString block under ``root``'s terraform tree."""
    findings: list[Finding] = []
    tf_root = root / "infra" / "terraform"
    if not tf_root.is_dir():
        return findings
    for tf_file in sorted(tf_root.rglob("*.tf")):
        if EXCLUDED_DIRS.intersection(tf_file.relative_to(root).parts):
            continue
        findings.extend(
            scan_text(str(tf_file.relative_to(root)), tf_file.read_text(encoding="utf-8"))
        )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO_ROOT), help="repo root to scan")
    args = parser.parse_args(argv)

    findings = check_repo(Path(args.root))
    if not findings:
        return 0
    for finding in findings:
        print(f"check_ssm_secret_state: {finding.render()}", file=sys.stderr)
    print(
        f"\ncheck_ssm_secret_state: {len(findings)} SecureString secret(s) persist a value to "
        "terraform state. terraform reads a plaintext `value` into state on every refresh even "
        "under `ignore_changes = [value]` (bug eb67-b96c-dcf0-4f86). Use write-only arguments "
        "`value_wo` + `value_wo_version` instead (ADR 0105).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
