#!/usr/bin/env python3
"""State-exposure gate for SSM SecureString secrets [rebar:eb67-b96c-dcf0-4f86].

An ``aws_ssm_parameter`` written with a plaintext ``value`` argument persists that value in
CLEARTEXT in the remote terraform state -- the AWS provider reads the live value into
``attributes.value`` on every refresh/import, even when ``lifecycle { ignore_changes = [value] }``
stops terraform WRITING it. For a ``SecureString`` secret that collapses the whole secret tier to
whoever can read the state backend (bug eb67: 23 secrets in cleartext in the prod state).

The structural fix (ADR 0105) is terraform WRITE-ONLY arguments: ``value_wo`` (+ its required
``value_wo_version``) is, by provider design, NEVER stored to state. This gate enforces that every
operator-seeded ``SecureString`` secret uses write-only args and none reintroduces the
persisted-secret antipattern.

The rule, per ``aws_ssm_parameter`` resource of ``type = "SecureString"``:

  * VIOLATION -- a quoted string-LITERAL ``value = "..."`` (a seeded secret whose value would land
    in state). This is the exact bug shape (``value = "CHANGEME"`` + ``ignore_changes = [value]``).
  * VIOLATION -- ``lifecycle { ignore_changes = [value] }`` on a SecureString (the tell-tale of a
    value terraform refreshes into state but pretends not to own).
  * VIOLATION -- ``value_wo`` without ``value_wo_version`` (or vice-versa): the provider requires
    the pair, and a lone ``value_wo`` never triggers the write.
  * VIOLATION -- no value source at all (neither ``value`` nor ``value_wo`` nor ``insecure_value``):
    an invalid resource that would fail apply, and usually a half-done migration.
  * ALLOWED -- ``value_wo`` + ``value_wo_version`` (the write-only fix).
  * ALLOWED -- ``value = <expression>`` (an unquoted reference such as
    ``random_password.x.result``) with NO ``ignore_changes`` on value: a terraform-GENERATED value.
    Its secret already lives in state via the source resource, so ``value_wo`` gives no net benefit;
    this is a deliberately different case (e.g. ``opcert_origin_guard``) and out of scope.

This is a HERMETIC static check -- it parses HCL text only, never contacts AWS, and never reads or
prints a secret value. It is the SSM-secret analogue of ``scripts/check_templatefile_escapes.py``
and is wired into ``make lint`` the same way.

Non-secret ``String``/``StringList`` params (e.g. ``jira-url``, ``jira-project``) are not secrets
and are ignored entirely.
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
_VALUE_RE = re.compile(r"\bvalue\s*=\s*(.)")

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


def _block_body(text: str, open_brace: int) -> str:
    """Return the ``{ ... }`` body starting at ``open_brace`` (the index of the ``{``)."""
    depth, j = 1, open_brace + 1
    while j < len(text) and depth:
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


def scan_text(tf_file: str, text: str) -> list[Finding]:
    """Findings for every ``aws_ssm_parameter`` SecureString block in one file's text."""
    findings: list[Finding] = []
    for res in _RESOURCE_RE.finditer(text):
        name = res.group(1)
        body = _block_body(text, text.index("{", res.start()))

        type_match = _TYPE_RE.search(body)
        if not type_match or type_match.group(1) != "SecureString":
            continue  # non-secret String/StringList params are out of scope

        value_match = _VALUE_RE.search(body)
        has_literal_value = bool(value_match and value_match.group(1) == '"')
        has_expr_value = bool(value_match and value_match.group(1) != '"')
        has_value_wo = bool(_VALUE_WO_RE.search(body))
        has_value_wo_version = bool(_VALUE_WO_VERSION_RE.search(body))
        has_insecure_value = bool(_INSECURE_VALUE_RE.search(body))
        ignores_value = _ignores_value(body)

        def add(problem: str, _file: str = tf_file, _name: str = name) -> None:
            findings.append(Finding(_file, _name, problem))

        if has_literal_value:
            add('persists a plaintext string-literal `value = "..."` into state')
        if ignores_value:
            add("uses `ignore_changes = [value]`, the persisted-secret antipattern")
        if has_value_wo and not has_value_wo_version:
            add("declares `value_wo` without the required `value_wo_version`")
        if has_value_wo_version and not has_value_wo:
            add("declares `value_wo_version` without `value_wo`")
        if not (has_value_wo or has_expr_value or has_literal_value or has_insecure_value):
            add("declares no value source (needs `value_wo` + `value_wo_version`)")
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
