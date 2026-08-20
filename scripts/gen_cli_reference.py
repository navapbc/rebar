#!/usr/bin/env python3
"""Generate ``docs/cli-reference.md`` — the canonical CLI command reference (ticket e866).

The reference is DERIVED from the CLI's own help data (like ``gen_env_registry.py``): a CI
drift gate regenerates it and fails the build on any diff, so a new command cannot ship
undocumented.

The CLI surface has two families, both emitted here:

  1. **Help-backed subcommands** — the dispatcher arms with pinned usage text under
     ``rebar/_cli/help/*.txt``, enumerated by ``rebar._cli._help.known_subcommands()`` and
     rendered verbatim via ``_help.subcommand_help(name)``.
  2. **Intercept-arm commands** — advanced commands handled before the dispatcher (each owns
     its own ``--help`` and has NO ``help/*.txt``). Their ``--help`` is not programmatically
     capturable (e.g. ``rebar enrich --help`` prints JSON, not usage), so each carries a
     curated one-liner in ``INTERCEPT_COMMANDS``. That key set is drift-gated against the
     intercept ladder in ``rebar._cli.__init__`` (``ladder_intercepts()``): a missing or
     stale curated entry makes ``render()`` raise loudly rather than emit a partial doc.

Usage:
    python scripts/gen_cli_reference.py            # regenerate docs/cli-reference.md
    python scripts/gen_cli_reference.py --check     # exit non-zero if the committed file is stale
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "cli-reference.md"
CLI_INIT = REPO_ROOT / "src" / "rebar" / "_cli" / "__init__.py"

# Curated one-line descriptions for intercept-arm commands. These commands own their
# own ``--help`` and have no pinned ``help/*.txt``, and their ``--help`` output is not usable
# programmatically (``rebar enrich --help`` prints JSON, others vary), so the descriptions are
# hand-maintained here. The key set is drift-gated against ``ladder_intercepts()``.
INTERCEPT_COMMANDS: dict[str, str] = {
    "audit": (
        "Show a ticket's audit trail: its full retained plan-review history, its completion "
        "attestation + sidecar record, and the associated code reviews "
        "(`audit show <ticket> [--output json|text]`)."
    ),
    "config": (
        "Show the resolved rebar configuration from the working-tree config files "
        "(a read-only config-transparency view; no store init)."
    ),
    "criteria": (
        "Run per-criterion calibration evals against the shared review-criteria registry."
    ),
    "enrich": (
        "Drain and report the cross-ticket overlap enrichment queue "
        "(`rebar enrich [--drain|--once|status]`)."
    ),
    "explain": ("Explain a review criterion by id — a pure registry/guide read, no LLM call."),
    "identity": (
        "Manage authenticated identities: create an identity entity, set the current "
        "self-identity, and add/revoke its signing keys (`identity key add|revoke`)."
    ),
    "jira-onboard": (
        "Compatibility alias for `rebar bridge setup`; routes through the same interactive "
        "Jira onboarding wizard."
    ),
    "llm": (
        "LLM-framework setup wizard for configuring the optional agent surfaces "
        "(API key, model, extras)."
    ),
    "prompt": "Run prompt-library evals over the packaged/overridden prompts.",
    "reconcile": (
        "Reconcile the rebar store with Jira (dry-run by default; `live` performs the sync)."
    ),
    "review-code": (
        "Run the LLM code-review agent over a diff or commit range and emit structured findings."
    ),
    "review-plan": (
        "Run the plan-review gate on a ticket; on a non-blocking PASS it signs the plan-review "
        "attestation the claim gate consumes. Signing is the DEFAULT — `--no-sign` is the "
        "explicit opt-out — and a BLOCK, an INDETERMINATE, or a degraded run is never signed. "
        "Recover a PASS whose signature was lost with `sign-review` (no LLM call)."
    ),
    "scan-spec": "Scan prose/spec text for spec-implied work in batches, emitting findings.",
    "sign-review": (
        "Re-sign a plan-review attestation from the last REVIEW_RESULT sidecar (cheap; no LLM "
        "call)."
    ),
    "verify-authorship": (
        "Back-compat alias for `verify-identity` (the authenticated-authorship merge-gate); "
        "dispatches identically."
    ),
    "verify-commit-ticket": (
        "Verify a commit message references a rebar ticket that resolves in the store "
        "(the commit-ticket gate)."
    ),
    "verify-completion": (
        "Run the completion-verifier agent to check a ticket's completion criteria are "
        "demonstrably met by the implementation."
    ),
    "verify-identity": (
        "The authenticated-authorship merge-gate: verify each mutating event's in-toto "
        "authorship signature against the author identity's commit-anchored keyring "
        "(`--require-authenticated`, `--since` grandfathering, `--format json` report)."
    ),
    "verify-opcert": (
        "The required-environment operation-certificate merge-gate: verify each in-scope closed "
        "ticket carries a valid completion-verifier op-cert from the trusted environment pinned in "
        "`.rebar/trusted_environments.yaml` (`--require-environment`, `--since` grandfathering)."
    ),
    "trusted-env": (
        "Maintain `.rebar/trusted_environments.yaml` (Option B): `add <env_id> <public_key>` and "
        "`revoke <env_id> <public_key-or-index>` stamp the current tickets-branch tip log position "
        "as the key's `added_at_log_position` / `revoked_at_log_position`."
    ),
    "remote-cert": (
        "Request an op-cert from the trusted gate service at `verify.opcert_remote_url` "
        "(SigV4-signed): submit `<ticket-id> <kind>`, poll to a verdict, and on PASS persist the "
        "returned signed envelope as a `SIGNATURE` event the merge gate certifies."
    ),
    "workflow": (
        "Author, dry-render, and run `.rebar/workflows/*.yaml` workflows (the workflow-engine "
        "DSL toolchain)."
    ),
}


# The mutating-verb record (ticket 0c00-0649-32da-41a5), curated here for the same
# reason as ``INTERCEPT_COMMANDS`` and drift-gated the same way: the key set MUST equal
# ``rebar._cli._CONFIRM_SCOPE`` (``_WRITES_FULL | _LIFECYCLE``), so a verb cannot join the
# confirmation channel without being classified. Each row carries
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


def ladder_intercepts() -> set[str]:
    """Return the intercept command names from the ``if argv[0] == "<name>"`` ladder in
    ``src/rebar/_cli/__init__.py``, parsed from source (never hardcoded — a newly-added
    intercept arm is detected automatically)."""
    source = CLI_INIT.read_text(encoding="utf-8")
    return set(re.findall(r'argv\[0\]\s*==\s*"([^"]+)"', source))


def render() -> str:
    """Build the full CLI-reference markdown.

    First runs a parity self-check: the curated ``INTERCEPT_COMMANDS`` key set MUST equal the
    intercept ladder parsed from source, else raise ``ValueError`` (a drifted/missing curated
    entry fails loudly rather than emitting a silently-incomplete doc). ``MUTATION_VERBS`` is
    checked the same way against the CLI's confirmation scope."""
    _check_mutation_verbs()
    ladder = ladder_intercepts()
    curated = set(INTERCEPT_COMMANDS)
    if curated != ladder:
        missing = ladder - curated
        extra = curated - ladder
        raise ValueError(
            "INTERCEPT_COMMANDS is out of sync with the intercept ladder in "
            f"src/rebar/_cli/__init__.py: missing curated entries {sorted(missing)}, "
            f"stale/extra curated entries {sorted(extra)}. "
            "Update INTERCEPT_COMMANDS in scripts/gen_cli_reference.py."
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
    lines.append(
        "The `rebar` CLI has two command families. **Help-backed subcommands** are the "
        "dispatcher arms with pinned usage text (rendered verbatim below). **Intercept-arm "
        "commands** are advanced commands handled before the dispatcher; each owns its own "
        "`--help` and is documented here by a curated one-liner — run `rebar <cmd> --help` "
        "for full usage."
    )
    lines.append(
        "Bridge operations use the canonical nested forms `rebar bridge fsck`, "
        "`rebar bridge check-access`, and `rebar bridge setup`; retained top-level "
        "spellings are identified below as compatibility aliases."
    )
    lines.append("")
    lines.append("## Mutation confirmations and global output flags")
    lines.append("")
    lines.append(
        "Every mutating verb (`create`, `idea`, `comment`, `link`, `unlink`, `revert`, "
        "`edit`, `tag`, `untag`, `archive`, `set-file-impact`, `set-verify-commands`, "
        "`attach-commits`, `session-log`, `transition`, `reopen`, `claim`) confirms its "
        "result on stdout with one kubectl-style line: `<past-tense-verb> <args-summary>` "
        "on a successful write, `no change: <reason>` on an idempotent no-op (exit 0 in "
        "both cases). Two global flags are extracted for these verbs at the top-level "
        "router (position-independent within the verb's arguments; tokens after `--` are "
        "never consumed):"
    )
    lines.append("")
    lines.append(
        "- `--quiet` / `-q` — suppress the text confirmation line only; errors, exit "
        "codes, JSON output, and `link`'s machine-readable REDIRECT record are untouched."
    )
    lines.append(
        "- `--output <text|json>` / `-o <mode>` — verbs that already accepted `--output` "
        "(`create`, `idea`, `transition`, `claim`, `reopen`) keep their pre-existing JSON "
        "shapes unchanged; the newly-covered verbs emit one uniform mutation envelope "
        '`{"outcome": "<verb-past>"|"noop", "subject": <id/edge>, "detail": <str>}` — '
        "**pre-1.0 UNSTABLE**: the envelope's field set may still change before 1.0. "
        "`--quiet` together with `--output json` still prints the JSON."
    )
    lines.append("")
    lines.extend(_mutation_tables())

    # ── Help-backed subcommands ──────────────────────────────────────────────
    subs = sorted(_help.known_subcommands())
    lines.append("## Help-backed subcommands")
    lines.append("")
    lines.append(
        f"The {len(subs)} subcommands with pinned help text "
        "(`rebar._cli._help.known_subcommands()`):"
    )
    lines.append("")
    for name in subs:
        lines.append(f"### `{name}`")
        lines.append("")
        help_text = _help.subcommand_help(name)
        body = (help_text or "").rstrip("\n")
        lines.append("```")
        lines.append(body)
        lines.append("```")
        lines.append("")

    # ── Intercept-arm commands ───────────────────────────────────────────────
    intercepts = sorted(INTERCEPT_COMMANDS)
    lines.append("## Intercept-arm commands")
    lines.append("")
    lines.append(
        f"The {len(intercepts)} advanced commands handled before the dispatcher. Each owns "
        "its own `--help` (no pinned help text); run `rebar <cmd> --help` for full usage."
    )
    lines.append("")
    lines.append("| Command | Description |")
    lines.append("|---------|-------------|")
    for name in intercepts:
        lines.append(f"| `{name}` | {INTERCEPT_COMMANDS[name]} |")
    lines.append("")

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
