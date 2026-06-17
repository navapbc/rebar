"""The rebar argparse CLI — the ``rebar`` entrypoint.

An in-process Python CLI. Its structure:

* ``argparse`` owns top-level tokenization (subcommand + REMAINDER); per-command
  flag parsing stays in each command's own implementation, so argument-error
  messages come from the unchanged impls.
* Help / overview / unknown-subcommand output comes from the pinned package-data
  strings in :mod:`rebar._cli._help`.
* Read and leaf-write commands dispatch **in-process** to
  ``rebar._engine_support.reads.main`` / ``rebar._commands.main`` with the
  per-command auto-init policy (:mod:`rebar._cli._init`).
* ``reconcile`` routes to ``python -m rebar_reconciler``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from rebar._cli import _help
from rebar._cli._init import ensure_initialized

# Read arms that auto-init only; the read path owns its own throttled reconverge.
_READS_INIT_ONLY = frozenset(
    {"show", "list", "list-epics", "next-batch", "deps", "ready", "search"}
)
# Read-compute arm the dispatcher ran with NO _ensure_initialized (self-manages).
_READS_NO_INIT = frozenset({"validate"})
# Field-read arms the dispatcher ran with FULL _ensure_initialized (ticket-lib-api.sh).
_FIELD_READS = frozenset({"get-file-impact", "get-verify-commands"})
# Resolution/display arms the dispatcher ran with FULL _ensure_initialized.
_LOOKUPS = frozenset({"exists", "resolve", "format"})
# Graph-traversal arm the dispatcher ran with FULL _ensure_initialized.
_DESCENDANTS = frozenset({"list-descendants"})
# Per-ticket gate arms the dispatcher ran with NO _ensure_initialized (they read
# transitively via `ticket show`, so the gate CLI itself does no auto-init).
_GATES = frozenset({"clarity-check", "check-ac", "quality-check", "summary"})
# Signature arms (native, no bash counterpart): `sign` is a write, `verify-signature`
# a read; both need an initialized store + the environment signing key.
_SIGNING = frozenset({"sign", "verify-signature"})
# Write/lifecycle arms (E3): full auto-init + reconverge before the in-process write.
_LIFECYCLE = frozenset({"transition", "reopen", "claim"})
# Compaction arms (E3): full auto-init before the in-process SNAPSHOT write.
_COMPACT = frozenset({"compact", "compact-all"})
# Bridge arms (E5): full auto-init UNLESS a tracker override is injected (test
# tracker), matching the dispatcher's `bridge-status`/`purge-bridge` arms.
_BRIDGE = frozenset({"bridge-status", "bridge-fsck", "purge-bridge"})
# Leaf-write arms: full auto-init + reconverge before the in-process write.
_WRITES_FULL = frozenset(
    {
        "create",
        "comment",
        "link",
        "unlink",
        "revert",
        "edit",
        "tag",
        "untag",
        "archive",
        "set-file-impact",
        "set-verify-commands",
    }
)


def _reconcile(argv: list[str]) -> int:
    """``rebar reconcile`` → ``python -m rebar_reconciler`` (mirrors cli.py)."""
    from rebar import config
    from rebar._engine import engine_env

    root = str(config.repo_root())
    args = list(argv)
    if not any(a == "--repo-root" or a.startswith("--repo-root=") for a in args):
        args += ["--repo-root", root]
    if not any(a == "--mode" or a.startswith("--mode=") for a in args):
        args += ["--mode", "dry-run"]
    # Launch under THIS interpreter (sys.executable), not a bare ``python3``: the
    # reconciler imports ``rebar.*`` in-package (Tier E E5b), so it needs the
    # rebar-capable interpreter; engine_env keeps the engine dir on PYTHONPATH so
    # the top-level ``rebar_reconciler`` package still resolves.
    return subprocess.call([sys.executable, "-m", "rebar_reconciler", *args], env=engine_env(root))


def _review(argv: list[str]) -> int:
    """``rebar review`` → rebar.llm.review_ticket (native; not a dispatcher arm).

    Like ``reconcile``, this is intercepted in main() before the bash-golden help
    system, so it owns its own ``--help``. JSON output conforms to the
    ``review_result`` schema (OUTPUT_SCHEMAS['review'])."""
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        prog="rebar review",
        description="Run an LLM review of a ticket (or its ticket-graph) and emit "
        "structured findings. Needs the 'agents' extra + a model API key (provider "
        "per REBAR_LLM_MODEL); see `rebar review --check`.",
    )
    parser.add_argument("ticket_id", nargs="?", help="ticket id, short id, or alias")
    parser.add_argument(
        "reviewer_id",
        nargs="?",
        default=None,
        help="reviewer from the catalog (default: the catalog's default reviewer)",
    )
    parser.add_argument(
        "--graph",
        action="store_true",
        help="also review the ticket's descendants, as one unit",
    )
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    parser.add_argument(
        "--check",
        action="store_true",
        help="print backend/credential availability and exit",
    )
    args = parser.parse_args(argv)

    from rebar import llm

    if args.check:
        sys.stdout.write(_json.dumps(llm.available_backends(), indent=2) + "\n")
        return 0
    if not args.ticket_id:
        parser.error("ticket_id is required")
    ensure_initialized(init_only=True)
    try:
        result = llm.review_ticket(args.ticket_id, args.reviewer_id, graph=args.graph)
    except llm.LLMError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    if args.output == "json":
        sys.stdout.write(_json.dumps(result) + "\n")
    else:
        _render_review_text(result)
    return 0


def _review_code(argv: list[str]) -> int:
    """``rebar review-code`` → rebar.llm.review_code (native, like reconcile/review).

    Reviews a git range (``--base``/``--head``) or a ``--diff-file`` with one or
    more reviewers; JSON output conforms to the ``review_result`` schema."""
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        prog="rebar review-code",
        description="Run an LLM code review of a change (git range or diff file) and "
        "emit aggregated structured findings. Needs the 'agents' extra + an API key.",
    )
    parser.add_argument("--base", default="HEAD~1", help="base git ref (default HEAD~1)")
    parser.add_argument("--head", default="HEAD", help="head git ref (default HEAD)")
    parser.add_argument("--diff-file", help="review this unified-diff file instead of a git range")
    parser.add_argument(
        "--reviewer",
        action="append",
        dest="reviewers",
        help="reviewer id (repeatable; default: deterministic selection)",
    )
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    args = parser.parse_args(argv)

    from rebar import llm

    diff_text = None
    if args.diff_file:
        try:
            with open(args.diff_file, encoding="utf-8", errors="replace") as fh:
                diff_text = fh.read()
        except OSError as exc:
            sys.stderr.write(f"Error: cannot read --diff-file: {exc}\n")
            return 1
    try:
        result = llm.review_code(
            base=args.base, head=args.head, diff_text=diff_text, reviewers=args.reviewers
        )
    except llm.LLMError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    if args.output == "json":
        sys.stdout.write(_json.dumps(result) + "\n")
    else:
        _render_review_text(result)
    return 0


def _scan_spec(argv: list[str]) -> int:
    """``rebar scan-spec`` → rebar.llm.scan_epics_for_spec (native op).

    Scans open epics against a spec for gaps/conflicts/overlaps; JSON output
    conforms to the ``review_result`` schema."""
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        prog="rebar scan-spec",
        description="Batch-scan open epics against a specification and emit "
        "structured findings (gaps/conflicts/overlaps). Needs the 'agents' extra.",
    )
    parser.add_argument("--spec-file", required=True, help="path to the specification text")
    parser.add_argument("--batch-size", type=int, default=5, help="epics per batch (default 5)")
    parser.add_argument(
        "--epic",
        action="append",
        dest="epics",
        help="restrict to these epic ids (repeatable; default: all open epics)",
    )
    parser.add_argument("--output", "-o", choices=["json", "text"], default="json")
    args = parser.parse_args(argv)

    try:
        with open(args.spec_file, encoding="utf-8", errors="replace") as fh:
            spec_text = fh.read()
    except OSError as exc:
        sys.stderr.write(f"Error: cannot read --spec-file: {exc}\n")
        return 1
    ensure_initialized(init_only=True)  # reads epics from the store
    from rebar import llm

    try:
        result = llm.scan_epics_for_spec(spec_text, epics=args.epics, batch_size=args.batch_size)
    except llm.LLMError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    if args.output == "json":
        sys.stdout.write(_json.dumps(result) + "\n")
    else:
        _render_review_text(result)
    return 0


def _render_review_text(result: dict) -> None:
    """Human-readable rendering of a review_result."""
    findings = result.get("findings", [])
    target = result.get("target", {})
    ids = ", ".join(target.get("ticket_ids", [])) or "?"
    sys.stdout.write(
        f"Review of {ids} ({result.get('runner')}/{result.get('model') or 'n/a'}) — "
        f"{len(findings)} finding(s)\n"
    )
    if result.get("summary"):
        sys.stdout.write(f"\n{result['summary']}\n")
    for f in findings:
        sys.stdout.write(f"\n[{f.get('severity', '?').upper()}] ({f.get('dimension')}) ")
        # Surface multi-reviewer consensus that aggregation computed (agreement>1).
        if f.get("agreement", 1) > 1:
            who = ", ".join(f.get("reviewers", [])) or "?"
            sys.stdout.write(f"[agreement {f['agreement']}: {who}] ")
        if f.get("title"):
            sys.stdout.write(f"{f['title']}\n")
        else:
            sys.stdout.write("\n")
        sys.stdout.write(f"  {f.get('detail', '')}\n")
        for c in f.get("citations", []):
            if c.get("kind") == "file":
                loc = c.get("path", "")
                if c.get("line_start"):
                    loc += f":{c['line_start']}"
                    if c.get("line_end") and c["line_end"] != c["line_start"]:
                        loc += f"-{c['line_end']}"
                sys.stdout.write(f"    @ {loc}\n")
            elif c.get("kind") == "url":
                sys.stdout.write(f"    @ {c.get('url', '')}\n")
            else:
                sys.stdout.write(f"    - {c.get('description', '')}\n")


def _bridge_probe(argv: list[str]) -> int:
    """``rebar bridge-probe`` → live Jira capability preflight.

    Launches the genuine python probe (``jira-capability-probe.py``) under
    ``sys.executable`` with ``engine_env`` (so the engine's
    ``rebar_reconciler.acli`` transport resolves) — replacing the bash-dispatcher
    passthrough (Tier E E6.5a). Talks only to Jira (creates + deletes a throwaway
    issue); needs no local tracker, so NO auto-init (matches the dispatcher arm).
    Output streams inherit so the operator sees the PROBE_PASS/FAIL lines directly.
    """
    from rebar._engine import engine_dir, engine_env

    script = str(engine_dir() / "jira-capability-probe.py")
    return subprocess.call([sys.executable, script, *argv], env=engine_env())


def _emit_subcommand_help(sub: str) -> int:
    """Print ``sub``'s usage (``_print_subcommand_help`` parity).

    Known subcommand → stdout, exit 0. Unknown → error + blank + overview all to
    stderr, exit 1 (the dispatcher's ``*)`` arm).
    """
    text = _help.subcommand_help(sub)
    if text is not None:
        sys.stdout.write(text)
        return 0
    sys.stderr.write(f"Error: unknown subcommand '{sub}'\n\n")
    sys.stderr.write(_help.overview())
    return 1


def _dispatch(sub: str, rest: list[str]) -> int:
    """Route a known subcommand to its in-process or passthrough implementation."""
    if sub == "init":
        # Explicit bootstrap — NEVER triggers auto-init (it IS init).
        from rebar._commands import init as _init_cmd

        return _init_cmd.init_cli(rest)
    if sub == "scratch":
        # Filesystem-only per-ticket store — NO auto-init (matches the dispatcher).
        from rebar._commands import scratch

        return scratch.scratch_cli(rest)
    if sub in _READS_INIT_ONLY:
        ensure_initialized(init_only=True)
        from rebar._engine_support import reads

        return reads.main([sub, *rest])
    if sub in _READS_NO_INIT:
        from rebar._engine_support import reads

        return reads.main([sub, *rest])
    if sub in _LIFECYCLE:
        ensure_initialized(init_only=False)
        from rebar._commands import transition as _transition

        if sub == "reopen":
            return _transition.reopen_cli(rest)
        if sub == "claim":
            return _transition.claim_cli(rest)
        return _transition.transition_cli(rest)
    if sub in _COMPACT:
        ensure_initialized(init_only=False)
        from rebar._commands import compact as _compact

        if sub == "compact-all":
            return _compact.compact_all_cli(rest)
        return _compact.compact_cli(rest)
    if sub == "delete":
        ensure_initialized(init_only=False)
        from rebar._commands import delete as _delete

        return _delete.delete_cli(rest)
    if sub in _BRIDGE:
        from rebar import config

        # The dispatcher auto-inits only when no test tracker is injected.
        if not config.tracker_dir_override():
            ensure_initialized(init_only=False)
        tracker = str(config.tracker_dir())
        if sub == "bridge-status":
            from rebar._engine_support import bridge

            return bridge.bridge_status_cli(rest, tracker)
        if sub == "bridge-fsck":
            from rebar._engine_support import bridge_fsck

            return bridge_fsck.main(rest)
        from rebar._commands import purge_bridge

        return purge_bridge.purge_bridge_cli(rest)
    if sub == "fsck":
        ensure_initialized(init_only=False)
        from rebar._commands import fsck as _fsck

        return _fsck.fsck_cli(rest)
    if sub == "fsck-recover":
        # The recover path resolves its own tracker (honors REBAR_TRACKER_DIR /
        # TICKETS_TRACKER_DIR / --tracker-dir); the dispatcher only auto-inits when
        # no tracker is injected.
        from rebar import config

        if not config.tracker_dir_override():
            ensure_initialized(init_only=False)
        from rebar._commands import fsck_recover as _fsck_recover

        return _fsck_recover.fsck_recover_cli(rest)
    if sub in _WRITES_FULL:
        ensure_initialized(init_only=False)
        from rebar._commands import main as commands_main

        return commands_main([sub, *rest])
    if sub in _FIELD_READS:
        ensure_initialized(init_only=False)
        from rebar._engine_support import field_reads, reads

        tracker = reads.tracker_dir()
        if sub == "get-file-impact":
            return field_reads.file_impact_cli(rest, tracker)
        return field_reads.verify_commands_cli(rest, tracker)
    if sub in _LOOKUPS:
        ensure_initialized(init_only=False)
        from rebar._engine_support import lookups, reads

        tracker = reads.tracker_dir()
        if sub == "exists":
            return lookups.exists_cli(rest, tracker)
        if sub == "resolve":
            return lookups.resolve_cli(rest, tracker)
        return lookups.format_cli(rest, tracker, os.path.dirname(tracker))
    if sub in _DESCENDANTS:
        ensure_initialized(init_only=False)
        from rebar._engine_support import descendants, reads

        return descendants.list_descendants_cli(rest, reads.tracker_dir())
    if sub in _GATES:
        from rebar._engine_support import gates, reads

        tracker = reads.tracker_dir()
        if sub == "check-ac":
            return gates.check_ac_cli(rest, tracker)
        if sub == "clarity-check":
            return gates.clarity_check_cli(rest, tracker, os.path.dirname(tracker))
        if sub == "quality-check":
            return gates.quality_check_cli(rest, tracker)
        return gates.summary_cli(rest, tracker)
    if sub in _SIGNING:
        ensure_initialized(init_only=False)
        from rebar import signing

        if sub == "sign":
            return signing.sign_cli(rest)
        return signing.verify_signature_cli(rest)
    if sub == "bridge-probe":
        return _bridge_probe(rest)
    # Every known subcommand is routed in-process above, and main() rejects
    # unknown subcommands before reaching _dispatch. Arriving here means a
    # subcommand was added to the known set without an in-process arm — a wiring
    # bug, surfaced loudly rather than silently mis-dispatched.
    raise RuntimeError(f"rebar: subcommand {sub!r} is known but has no in-process handler")


def main(argv: list[str] | None = None) -> int:
    """rebar CLI entry. Returns the process exit code.

    Control flow mirrors the bash dispatcher's help-interception-before-dispatch
    order so no command is executed on a help request and the streams/exit codes
    match the pinned goldens.
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    # Global config overrides (git -c style): `rebar -c section.key=value [...] <cmd>`,
    # repeatable, BEFORE the subcommand. They install the highest-precedence `cli`
    # layer (CLI > env > project > user > defaults) for every config consumer this
    # invocation — the verify gate, push/pull policy, display mode, etc.
    _overrides: list[str] = []
    while argv and (argv[0] in ("-c", "--config") or argv[0].startswith("--config=")):
        tok = argv.pop(0)
        if tok.startswith("--config="):
            _overrides.append(tok[len("--config=") :])
        elif argv:
            _overrides.append(argv.pop(0))
        else:
            sys.stderr.write(f"Error: {tok} requires a SECTION.KEY=VALUE argument\n")
            return 1
    if _overrides:
        from rebar import config as _config

        try:
            _config.set_cli_overrides(_config.parse_cli_overrides(_overrides))
        except _config.ConfigError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1

    # reconcile intercept (the dispatcher has no reconcile arm).
    if argv and argv[0] == "reconcile":
        return _reconcile(argv[1:])

    # review intercept (native rebar.llm op; not a dispatcher arm, like reconcile).
    if argv and argv[0] == "review":
        return _review(argv[1:])

    # review-code intercept (native rebar.llm code-review op).
    if argv and argv[0] == "review-code":
        return _review_code(argv[1:])

    # scan-spec intercept (native rebar.llm batch spec-scan op).
    if argv and argv[0] == "scan-spec":
        return _scan_spec(argv[1:])

    # config intercept (native config-transparency read; owns its own --help, like
    # reconcile/review). No store init: it reads working-tree config files only.
    if argv and argv[0] == "config":
        from rebar._commands import show_config

        return show_config.config_cli(argv[1:])

    # No subcommand: overview to stdout, exit 1 (the dispatcher's _usage).
    if not argv:
        sys.stdout.write(_help.overview())
        return 1

    first = argv[0]

    # Top-level help: `rebar help [<sub>]`, `rebar --help`, `rebar -h`.
    if first in ("help", "--help", "-h"):
        if len(argv) >= 2:
            return _emit_subcommand_help(argv[1])
        sys.stdout.write(_help.overview())
        return 0

    sub, rest = first, argv[1:]

    # `rebar <sub> --help|-h` as the FIRST arg after the subcommand → usage, no exec.
    if rest and rest[0] in ("--help", "-h"):
        return _emit_subcommand_help(sub)

    # Unknown subcommand: error to stderr + overview to stdout, exit 1.
    if sub not in _help.known_subcommands():
        sys.stderr.write(f"Error: unknown subcommand '{sub}'\n")
        sys.stdout.write(_help.overview())
        return 1

    return _dispatch(sub, rest)


# argparse scaffold — a real ArgumentParser owns top-level tokenization. Help and
# errors are intercepted in main() so the byte-exact dispatcher contract wins; the
# parser exists so the CLI is argparse-structured and gains its tokenization.
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rebar", add_help=False)
    parser.add_argument("subcommand", nargs="?")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    return parser


if __name__ == "__main__":
    sys.exit(main())
