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

# Curated one-line descriptions for the 16 intercept-arm commands. These commands own their
# own ``--help`` and have no pinned ``help/*.txt``, and their ``--help`` output is not usable
# programmatically (``rebar enrich --help`` prints JSON, others vary), so the descriptions are
# hand-maintained here. The key set is drift-gated against ``ladder_intercepts()``.
INTERCEPT_COMMANDS: dict[str, str] = {
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
        "Interactive Jira onboarding wizard: detect, prompt for, persist, and validate "
        "the Jira connection settings."
    ),
    "llm": (
        "LLM-framework setup wizard for configuring the optional agent surfaces "
        "(API key, model, extras)."
    ),
    "prompt": "Run prompt-library evals over the packaged/overridden prompts.",
    "reconcile": (
        "Reconcile the rebar store with Jira (dry-run by default; `live` performs the sync)."
    ),
    "review": (
        "Run the tool-using LLM agent to review a ticket (or its graph) and emit structured "
        "findings."
    ),
    "review-code": (
        "Run the LLM code-review agent over a diff or commit range and emit structured findings."
    ),
    "review-plan": (
        "Run the plan-review gate on a ticket; on a non-blocking PASS it signs the plan-review "
        "attestation the claim gate consumes."
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
    entry fails loudly rather than emitting a silently-incomplete doc)."""
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
    lines.append("")

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
