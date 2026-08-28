#!/usr/bin/env python3
"""Escape gate for files consumed by Terraform's ``templatefile()`` [rebar:dd30-f10d-69f3-4c36].

``templatefile()`` interpolates the **whole** file as an HCL template. Shell (``#``), JS
(``//``) and every other comment syntax is invisible to it: a comment line is template text
like any other. ``$${`` is the escape for a literal ``${``; an unescaped ``${name}`` is an
interpolation whose root identifier must resolve against the call's variable map.

The motivating defect (bug dd30) is exactly that gap. Commit ``ef1a7e66a65d`` added explanatory
comments to ``infra/terraform/user_data.sh`` that escaped the FIRST mention of a bash brace
expansion as ``$${...}`` and left the "reduces to" half unescaped::

    # PARAMS is consumed below as $${!PARAMS[@]} / $${PARAMS[$name]}, which templatefile
    # reduces to ${!PARAMS[@]} / ${PARAMS[$name]}.   <-- interpolated, not text

Terraform then parsed ``!PARAMS[@]`` as an HCL expression and rejected the ``!``, which broke
**every** terraform operation on the repo — ``-target`` does not help, because terraform
evaluates the entire configuration before honouring it.

**ShellCheck cannot catch this class.** The file is valid bash; the breakage is in a different
consumer. That is the whole reason this gate exists alongside ``check_shellcheck.py``.

The rule is deliberately **declared-variable-aware**, not a blanket ban on ``${``. The same file
contains four unescaped ``${data_volume_id}`` references that are legitimate and load-bearing —
``data_volume_id`` is the one variable ``main.tf`` passes. A gate that rejected every ``${``
would reject the feature along with the defect. So: an interpolation is a finding when its root
identifier is neither a declared variable of that call site nor a Terraform builtin.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Directories never scanned (vendored / generated / scratch), mirroring check_shellcheck.py.
EXCLUDED_DIRS = frozenset(
    {
        ".venv",
        ".git",
        # `terraform init` vendors provider and module sources here. Those templates are not
        # ours, are not covered by our variable maps, and would flood the gate with findings
        # nobody in this repo can act on.
        ".terraform",
        ".claude",
        ".tickets-tracker",
        ".tickets-hotpath-authoritative",
        "node_modules",
    }
)

#: Roots that are always resolvable inside a template without being declared: Terraform's own
#: namespaces. A template referencing these is unusual but not a defect of this class.
BUILTIN_ROOTS = frozenset({"path", "var", "local", "each", "count", "terraform", "self", "module"})

#: An identifier immediately followed by `(` is a FUNCTION call, not a variable reference —
#: `${jsonencode(signing_secret)}` references `signing_secret`, not `jsonencode`.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")

#: `templatefile(` followed by a quoted path literal, then a `{ ... }` variable map.
_TEMPLATEFILE = re.compile(r"templatefile\(\s*\"([^\"]*)\"\s*,", re.MULTILINE)

#: `${path.module}` / `${var.source_root}` style prefixes inside a path literal.
_PATH_INTERP = re.compile(r"\$\{[^}]*\}")


@dataclass(frozen=True)
class Unresolved:
    """A ``templatefile()`` call whose path could not be resolved, so it was never checked.

    Reported rather than skipped: an unchecked call site is indistinguishable from a clean one
    in the exit code, which is exactly how a guard becomes vacuous.
    """

    tf_file: str
    raw_path: str

    def render(self) -> str:
        return (
            f"{self.tf_file}: could not resolve templatefile() path {self.raw_path!r}, so this "
            "call site was NOT checked. Make the path resolvable (or exclude the directory) -- "
            "an unchecked call site must not pass silently."
        )


@dataclass(frozen=True)
class Finding:
    """One unescaped interpolation whose root identifier is not in scope."""

    template: str
    line: int
    col: int
    root: str
    text: str

    def render(self) -> str:
        return (
            f"{self.template}:{self.line}:{self.col}: unescaped '${{' referencing undeclared "
            f"'{self.root}' -- write '$${{' to keep it literal.\n    {self.text.strip()[:120]}"
        )


def scan_interpolations(text: str) -> list[tuple[int, int, str]]:
    """Return (line, col, body) for each UNESCAPED ``${...}`` / ``%{...}`` in ``text``.

    Escape-aware: ``$$`` and ``%%`` are consumed as literal-escapes, so ``$${x}`` is correctly
    treated as text. A naive ``grep '\\${'`` cannot make that distinction and would flag the
    escaped form the file uses everywhere.
    """
    out: list[tuple[int, int, str]] = []
    i, n = 0, len(text)
    while i < n - 1:
        char, nxt = text[i], text[i + 1]
        if char in "$%":
            if nxt == char:  # `$$` / `%%` -> escaped literal; consume both
                i += 2
                continue
            if nxt == "{":
                depth, j = 1, i + 2
                while j < n and depth:
                    if text[j] == "{":
                        depth += 1
                    elif text[j] == "}":
                        depth -= 1
                    j += 1
                line = text.count("\n", 0, i) + 1
                out.append((line, i - text.rfind("\n", 0, i), text[i + 2 : j - 1]))
                i = j
                continue
        i += 1
    return out


#: HCL template-directive keywords. `%{ if x }` / `%{ for k, v in m }` are CONTROL FLOW, not
#: variable references, so their keywords must never be reported as undeclared roots.
_DIRECTIVE_KEYWORDS = frozenset({"if", "for", "in", "else", "endif", "endfor"})

#: `%{ for k, v in map }` — the names between `for` and `in` are loop-LOCAL bindings introduced
#: by the directive itself, so they are in scope for its body and are not template variables.
_FOR_BINDINGS = re.compile(r"^\s*for\s+(.+?)\s+in\s", re.S)


def referenced_roots(body: str) -> list[str]:
    """Root identifiers a template expression READS, ignoring function names and directives."""
    roots: list[str] = []
    skip = set(_DIRECTIVE_KEYWORDS)
    bindings = _FOR_BINDINGS.match(body)
    if bindings:
        skip |= {name.strip() for name in bindings.group(1).split(",") if name.strip()}
    for match in _IDENT.finditer(body):
        if match.group(0) in skip:
            continue
        after = body[match.end() :].lstrip()
        if after.startswith("("):  # a function call, not a variable
            continue
        before = body[: match.start()].rstrip()
        if before.endswith("."):  # an attribute of an already-counted root
            continue
        roots.append(match.group(0))
    return roots


def resolve_template_path(raw: str, tf_file: Path, root: Path) -> Path | None:
    """Resolve a ``templatefile()`` path literal to a real file, or None if it cannot be found.

    ``${path.module}`` resolves to the calling ``.tf``'s directory. Anything else (typically
    ``${var.source_root}``, whose value lives in a module call this repo may not even
    instantiate) is unresolvable statically, so fall back to a unique suffix match over the
    tree -- better than silently skipping a template.
    """
    if raw.startswith("${path.module}"):
        candidate = tf_file.parent / raw[len("${path.module}") :].lstrip("/")
        return candidate if candidate.is_file() else None
    if "${" not in raw:
        candidate = tf_file.parent / raw
        return candidate if candidate.is_file() else None
    suffix = _PATH_INTERP.sub("", raw).lstrip("/")
    if not suffix:
        return None
    matches = [
        p
        for p in root.rglob("*" + Path(suffix).name)
        if not EXCLUDED_DIRS.intersection(p.relative_to(root).parts)
        and p.is_file()
        and str(p).endswith(suffix)
    ]
    return matches[0] if len(matches) == 1 else None


def declared_vars(text: str, start: int) -> set[str]:
    """Variable names declared in the ``{ ... }`` map that follows a ``templatefile(`` call."""
    open_brace = text.find("{", start)
    if open_brace < 0:
        return set()
    depth, j = 1, open_brace + 1
    while j < len(text) and depth:
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
        j += 1
    return {m.group(1) for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_-]*)\s*=", text[open_brace:j])}


#: What the gate reports: a bad interpolation, or a call site it could not check at all. Both
#: render to a line and both fail the build -- an unchecked site must not read as a clean one.
Report = Finding | Unresolved


def check_repo(root: Path) -> list[Report]:
    """Every ``templatefile()`` call site in ``root``, checked against its own variable map."""
    findings: list[Report] = []
    for tf_file in sorted(root.rglob("*.tf")):
        if EXCLUDED_DIRS.intersection(tf_file.relative_to(root).parts):
            continue
        tf_text = tf_file.read_text(encoding="utf-8")
        for call in _TEMPLATEFILE.finditer(tf_text):
            template = resolve_template_path(call.group(1), tf_file, root)
            if template is None:
                # A call site the gate cannot resolve is a call site the gate cannot CHECK.
                # Skipping it silently would let the guard exit 0 while leaving the very
                # breakage it exists to catch unexamined -- a vacuous pass.
                findings.append(Unresolved(str(tf_file.relative_to(root)), call.group(1)))
                continue
            allowed = declared_vars(tf_text, call.end()) | BUILTIN_ROOTS
            body_text = template.read_text(encoding="utf-8")
            lines = body_text.split("\n")
            rel = template.relative_to(root)
            # `%{ for k, v in items }` binds k and v for the REST of the loop, and those
            # bindings are read by SEPARATE interpolations (`${k}`) that carry no trace of the
            # directive. Scope therefore has to be tracked across the template, not per
            # expression; a stack handles nesting.
            scopes: list[set[str]] = []
            for line, col, expr in scan_interpolations(body_text):
                bindings = _FOR_BINDINGS.match(expr)
                if bindings:
                    scopes.append(
                        {name.strip() for name in bindings.group(1).split(",") if name.strip()}
                    )
                elif expr.strip() == "endfor" and scopes:
                    scopes.pop()
                in_scope = allowed.union(*scopes) if scopes else allowed
                for name in referenced_roots(expr):
                    if name not in in_scope:
                        findings.append(Finding(str(rel), line, col, name, lines[line - 1]))
                        break
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO_ROOT), help="repo root to scan")
    args = parser.parse_args(argv)

    findings = check_repo(Path(args.root))
    if not findings:
        return 0
    for finding in findings:
        print(f"check_templatefile_escapes: {finding.render()}", file=sys.stderr)
    print(
        f"\ncheck_templatefile_escapes: {len(findings)} unescaped interpolation(s). "
        "templatefile() interpolates the WHOLE file -- comments included -- so an unescaped "
        "'${...}' naming anything the call does not declare breaks EVERY terraform operation "
        "(bug dd30-f10d-69f3-4c36). Escape it as '$${...}', or reword to avoid the sequence.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
