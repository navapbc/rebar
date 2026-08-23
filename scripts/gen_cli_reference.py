#!/usr/bin/env python3
"""Generate ``docs/cli-reference.md`` — the canonical CLI command reference (ticket 6755).

Every command syntax and options section is derived from the immutable route registry
(:mod:`rebar._cli._registry`) and the committed package help. The help generator proves byte
currency through ``scripts/gen_cli_help.py --check``. A CI drift gate regenerates this file
and fails the build on any difference, so a new command cannot ship undocumented.

Each visible route that is not retired gets one ``### `<name>``` syntax section in registry
order. Every section embeds the committed bytes returned by
``rebar._cli._help.subcommand_help(name)``.

The verb-confirmation record (``MUTATION_VERBS``) is a behavioral output record — NOT a
syntax census — and stays curated here, drift-gated against ``rebar._cli._CONFIRM_SCOPE``.

Cross-command rationale lives in :data:`EDITORIAL_PREAMBLE`, kept structurally separate from
the generated syntax sections and validated by :func:`lint_editorial` (an editorial block may
never assert its own usage grammar or reference an unknown top-level command).

Usage:
    python scripts/gen_cli_reference.py            # regenerate docs/cli-reference.md
    python scripts/gen_cli_reference.py --check     # exit non-zero if the committed file is stale
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from rebar._cli._registry import ROUTES

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "cli-reference.md"


# Cross-command rationale: the mutation-confirmation / ``--quiet`` / ``--output`` explanation
# and the bridge nested-forms note. Prose only — NO ``Usage:`` line and NO ``| Option |``
# table (parser artifacts are the only usage authority), and every ``rebar <cmd>`` reference
# names a live route. :func:`render` runs :func:`lint_editorial` over this and fails loudly if
# either rule is broken.
EDITORIAL_PREAMBLE = """\
The **Command syntax** section embeds the committed package help for every visible command. \
The route registry determines section order and the help generator derives each artifact \
from its parser factory.

Every mutating verb (`create`, `idea`, `comment`, `link`, `unlink`, `revert`, `edit`, `tag`,
`untag`, `archive`, `set-file-impact`, `set-verify-commands`, `attach-commits`, `session-log`,
`transition`, `reopen`, `claim`) confirms its result on stdout with one kubectl-style line:
`<past-tense-verb> <args-summary>` on a successful write, `no change: <reason>` on an
idempotent no-op (exit 0 in both cases). Two global flags are extracted for these verbs at the
top-level router (position-independent within the verb's arguments; tokens after `--` are
never consumed):

- `--quiet` / `-q` — suppress the text confirmation line only; errors, exit codes, JSON
  output, and `link`'s machine-readable REDIRECT record are untouched.
- `--output <text|json>` / `-o <mode>` — verbs that already accepted `--output` (`create`,
  `idea`, `transition`, `claim`, `reopen`) keep their pre-existing JSON shapes unchanged; the
  newly-covered verbs emit one uniform mutation envelope
  `{"outcome": "<verb-past>"|"noop", "subject": <id/edge>, "detail": <str>}` — **pre-1.0
  UNSTABLE**: the envelope's field set may still change before 1.0. `--quiet` together with
  `--output json` still prints the JSON.

Bridge operations use the canonical nested forms `rebar bridge fsck`, `rebar bridge
check-access`, and `rebar bridge setup`; the retained top-level `rebar bridge-fsck` spelling is
kept for compatibility.
"""


# The mutating-verb record (ticket 0c00-0649-32da-41a5), curated here and drift-gated: the
# key set MUST equal ``rebar._cli._CONFIRM_SCOPE`` (``_WRITES_FULL | _LIFECYCLE``), so a verb
# cannot join the confirmation channel without being classified. Each row carries
#
# * ``noop``      — whether the verb has an idempotent no-op path (``no change: …``,
#                   exit 0, zero events written) or always writes;
# * ``condition`` — what makes it a no-op, or why every invocation writes;
# * ``old``       — the verb's stdout BEFORE the confirmation channel landed
#                   (commit 42cd70b889), the golden record of what was normalized away;
# * ``new``       — the confirmation line(s) it prints today.
MUTATION_VERBS: dict[str, dict[str, object]] = {
    "create": {
        "noop": False,
        "condition": "every invocation appends a CREATE event",
        "old": "`Created ticket <alias> (<id>): <title>` plus a second, bare `<id>` line",
        "new": "`created <alias> (<id>): <title>`",
    },
    "idea": {
        "noop": False,
        "condition": "every invocation appends a CREATE event of type `idea`",
        "old": "`Created idea <alias> (<id>): <title>` plus a second, bare `<id>` line",
        "new": "`created idea <alias> (<id>): <title>`",
    },
    "comment": {
        "noop": False,
        "condition": "every invocation appends a COMMENT event",
        "old": "(silent)",
        "new": "`comment added to <id>`",
    },
    "link": {
        "noop": True,
        "condition": "the identical edge (same source, target, relation) already exists",
        "old": "(silent; only the machine-readable REDIRECT record when the hierarchy "
        "resolver promotes the edge)",
        "new": "`linked <src> -> <dst> (<relation>)` / "
        "`no change: already linked <src> -> <dst> (<relation>)`",
    },
    "unlink": {
        "noop": False,
        "condition": "removing an absent edge is an error, not a no-op",
        "old": "(silent)",
        "new": "`unlinked <src> -/-> <dst> (<relation>)`",
    },
    "revert": {
        "noop": False,
        "condition": "every invocation appends a REVERT event",
        "old": "`Reverted event '<uuid>' on ticket '<id>'`",
        "new": "`reverted <id>: event <uuid>`",
    },
    "edit": {
        "noop": False,
        "condition": "every invocation appends an EDIT event for the named fields",
        "old": "(silent)",
        "new": "`edited <id>: <field names>` (field NAMES only, never their values)",
    },
    "tag": {
        "noop": True,
        "condition": "the tag is already on the ticket",
        "old": "(silent)",
        "new": "`tagged <id>: +<tag>` / `no change: tag <tag> already on <id>`",
    },
    "untag": {
        "noop": True,
        "condition": "the tag is not on the ticket",
        "old": "(silent)",
        "new": "`untagged <id>: -<tag>` / `no change: tag <tag> not on <id>`",
    },
    "archive": {
        "noop": True,
        "condition": "the ticket is already archived",
        "old": "(silent)",
        "new": "`archived <id>` / `no change: <id> already archived`",
    },
    "set-file-impact": {
        "noop": False,
        "condition": "every invocation appends the declaration event",
        "old": "(silent)",
        "new": "`impact set on <id>: <n> paths` / `impact set on <id>: none declared`",
    },
    "set-verify-commands": {
        "noop": False,
        "condition": "every invocation appends the declaration event",
        "old": "(silent)",
        "new": "`verify-commands set on <id>: <n>`",
    },
    "attach-commits": {
        "noop": False,
        "condition": "every invocation appends the attachment event",
        "old": "(silent)",
        "new": "`commits attached to <id>: <n>`",
    },
    "session-log": {
        "noop": False,
        "condition": "every invocation starts a log or appends an entry to one",
        "old": "the raw JSON result document",
        "new": "`session-log started <alias> (<id>)` / `session-log appended to <id>`",
    },
    "transition": {
        "noop": True,
        "condition": "the ticket is already at the target status",
        "old": "`UNBLOCKED: <ids|none>` on a close only (silent on every other target); "
        "`No transition needed` on a no-op",
        "new": "`transitioned <id>: <from> -> <to>; unblocked: <ids|none>` / "
        "`no change: <id> already <status>`",
    },
    "reopen": {
        "noop": False,
        "condition": "reopening a ticket that is not closed is a status mismatch, not a no-op",
        "old": "(silent — it delegated to `transition <id> closed open`, whose text output "
        "was emitted on closes only)",
        "new": "`reopened <id>: closed -> open`",
    },
    "claim": {
        "noop": False,
        "condition": "claiming an already-claimed ticket is a concurrency error (exit 10), "
        "not a no-op",
        "old": "`CLAIMED: <id> (assignee: <who>)`",
        "new": "`claimed <id>: open -> in_progress (assignee <who>)`",
    },
}


def _check_mutation_verbs() -> None:
    """Fail loudly when ``MUTATION_VERBS`` drifts from the CLI's confirmation scope."""
    from rebar._cli import _CONFIRM_SCOPE

    curated = set(MUTATION_VERBS)
    if curated != set(_CONFIRM_SCOPE):
        missing = sorted(set(_CONFIRM_SCOPE) - curated)
        extra = sorted(curated - set(_CONFIRM_SCOPE))
        raise ValueError(
            "MUTATION_VERBS is out of sync with rebar._cli._CONFIRM_SCOPE "
            f"(_WRITES_FULL | _LIFECYCLE): unclassified verbs {missing}, "
            f"stale/extra curated entries {extra}. "
            "Classify the verb in MUTATION_VERBS in scripts/gen_cli_reference.py."
        )


def _cell(value: object) -> str:
    """Escape a table cell: a literal `|` (e.g. `<ids|none>`) would split the column."""
    return str(value).replace("|", "\\|")


def _mutation_tables() -> list[str]:
    """Render the verb inventory and the confirmation-line migration record."""
    lines = ["### Verb inventory: no-op-capable vs always-writes", ""]
    lines.append(
        "Which verbs can report `no change: <reason>` (an idempotent re-run writes no event "
        "and still exits 0) and which write on every invocation. This table is generated from "
        "`MUTATION_VERBS` in the generator, whose key set is checked against "
        "`rebar._cli._CONFIRM_SCOPE`: a verb added to the confirmation channel without a "
        "classification fails the drift gate."
    )
    lines.append("")
    lines.append("| Verb | Classification | No-op condition / why it always writes |")
    lines.append("|------|----------------|----------------------------------------|")
    for name, row in sorted(MUTATION_VERBS.items()):
        kind = "no-op-capable" if row["noop"] else "always-writes"
        lines.append(f"| `{name}` | {kind} | {_cell(row['condition'])} |")
    lines.append("")
    lines.append("### Confirmation-line record (pre-`42cd70b889` → today)")
    lines.append("")
    lines.append(
        "The golden record of the normalization: what each verb printed before the "
        "confirmation channel landed, and what it prints now. Every datum of the old form "
        "survives in the new line (the per-verb exact-line assertions live in "
        "`tests/unit/test_mutation_confirmations.py`)."
    )
    lines.append("")
    lines.append("| Verb | Before | Today |")
    lines.append("|------|--------|-------|")
    for name, row in sorted(MUTATION_VERBS.items()):
        lines.append(f"| `{name}` | {_cell(row['old'])} | {_cell(row['new'])} |")
    lines.append("")
    return lines


def _live_route_names() -> set[str]:
    """The top-level spellings a `rebar <cmd>` reference may name: every non-retired route."""
    return {route.name for route in ROUTES if not route.retired}


_REBAR_REF = re.compile(r"`rebar\s+([A-Za-z0-9][A-Za-z0-9-]*)")
_OPTION_HEADER = re.compile(r"^\s*\|\s*option\b", re.IGNORECASE)


def lint_editorial(text: str) -> list[str]:
    """Return deterministic editorial findings (empty == clean) for an editorial prose block.

    Flags, as separate findings: (a) a line asserting its own ``usage:`` grammar; (b) a
    markdown option-table header; (c) a ``rebar <word>`` reference whose first word is not a
    live (non-retired) route name."""
    findings: list[str] = []
    live = _live_route_names()
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.strip().lower().startswith("usage:"):
            findings.append(
                f"line {lineno}: editorial prose asserts a `usage:` grammar — "
                "parser artifacts are the only usage authority"
            )
        if _OPTION_HEADER.match(line):
            findings.append(
                f"line {lineno}: editorial prose renders an option table header — "
                "parser artifacts are the only options authority"
            )
    for word in _REBAR_REF.findall(text):
        if word not in live:
            findings.append(
                f"`rebar {word}` references an unknown top-level command "
                "(not a live route in ROUTES)"
            )
    return findings


def _documented_routes() -> list:
    """Return visible routes that are not retired in registry order."""
    return [r for r in ROUTES if not r.retired and not r.hidden]


def _syntax_body(route, help_mod) -> str:
    """Return one route's committed help bytes for its fenced syntax body."""
    body = help_mod.subcommand_help(route.name)
    if body is None:
        raise ValueError(
            f"documented route {route.name!r} has no committed help bytes. "
            "Regenerate with `python scripts/gen_cli_help.py`."
        )
    return body.rstrip("\n")


def _syntax_sections(help_mod) -> list[str]:
    """Emit exactly one ``### `<name>``` fenced syntax block per documented route."""
    lines: list[str] = []
    for route in _documented_routes():
        lines.append(f"### `{route.name}`")
        lines.append("")
        lines.append("```")
        lines.append(_syntax_body(route, help_mod))
        lines.append("```")
        lines.append("")
    return lines


def render() -> str:
    """Build the full CLI-reference markdown from the registry + committed help bytes.

    Runs the ``MUTATION_VERBS`` drift check and the editorial linter first (raising
    ``ValueError`` on either), then assembles: banner → editorial preamble → mutation
    confirmations → per-route syntax sections."""
    _check_mutation_verbs()
    editorial_findings = lint_editorial(EDITORIAL_PREAMBLE)
    if editorial_findings:
        raise ValueError(
            "EDITORIAL_PREAMBLE failed the editorial lint in scripts/gen_cli_reference.py: "
            + "; ".join(editorial_findings)
        )

    from rebar._cli import _help

    lines: list[str] = []
    lines.append("# CLI command reference")
    lines.append("")
    lines.append(
        "**Generated by `scripts/gen_cli_reference.py` — do not edit by hand.** Run "
        "`python scripts/gen_cli_reference.py` to regenerate; a CI drift gate fails the "
        "build if this file is stale."
    )
    lines.append("")
    lines.extend(EDITORIAL_PREAMBLE.rstrip("\n").splitlines())
    lines.append("")
    lines.append("## Mutation confirmations and global output flags")
    lines.append("")
    lines.extend(_mutation_tables())
    lines.append("## Command syntax")
    lines.append("")
    lines.append(
        f"One section per visible command that is not retired "
        f"({len(_documented_routes())} in total), in registry order. Every section embeds "
        "its committed package help bytes."
    )
    lines.append("")
    lines.extend(_syntax_sections(_help))

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the CLI command reference.")
    parser.add_argument(
        "--check", action="store_true", help="exit non-zero if the committed file is stale"
    )
    args = parser.parse_args(argv)
    generated = render()
    if args.check:
        current = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
        if current != generated:
            sys.stderr.write(
                "docs/cli-reference.md is stale — regenerate with "
                "`python scripts/gen_cli_reference.py`\n"
            )
            return 1
        return 0
    DOC_PATH.write_text(generated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
