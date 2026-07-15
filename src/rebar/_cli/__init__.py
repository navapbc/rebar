"""The rebar argparse CLI — the ``rebar`` entrypoint.

An in-process Python CLI. Its structure:

* ``main()`` owns top-level tokenization (subcommand + remainder args); per-command
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

import os
import subprocess
import sys

from rebar._cli import _help
from rebar._cli._init import ensure_initialized
from rebar._cli._llm_commands import (
    _criteria,
    _explain,
    _llm,
    _prompt,
    _review,
    _review_code,
    _review_plan,
    _scan_spec,
    _sign_review,
    _verify_completion,
)
from rebar._cli._workflow_commands import _workflow

# Read arms that auto-init only; the read path owns its own throttled reconverge.
_READS_INIT_ONLY = frozenset(
    {"show", "list", "next-batch", "deps", "ready", "search", "session-logs"}
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
# Import/export arms (P1.2): NDJSON interop projection. `export` is a read
# (init-only); `import` composes writes (full init).
_IO = frozenset({"export", "import"})
# Leaf-write arms: full auto-init + reconverge before the in-process write.
_WRITES_FULL = frozenset(
    {
        "create",
        "idea",
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
        "session-log",
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


def _bridge_probe(argv: list[str], *, extra_env: dict[str, str] | None = None) -> int:
    """``rebar bridge-probe`` → live Jira capability preflight.

    Launches the genuine python probe (``jira-capability-probe.py``) under
    ``sys.executable`` with ``engine_env`` (so the engine's
    ``rebar_reconciler.acli`` transport resolves) — replacing the bash-dispatcher
    passthrough (Tier E E6.5a). Talks only to Jira (creates + deletes a throwaway
    issue); needs no local tracker, so NO auto-init (matches the dispatcher arm).
    Output streams inherit so the operator sees the PROBE_PASS/FAIL lines directly.

    ``extra_env`` overlays additional variables onto ``engine_env()`` before launch,
    and these **override** any same-named variable inherited from ``os.environ``
    (``{**engine_env(), **extra_env}`` — last writer wins). The probe reads
    ``JIRA_URL`` / ``JIRA_USER`` / ``JIRA_PROJECT`` from its process env (not from
    ``load_config()``), so ``rebar jira-onboard`` passes the just-persisted,
    config-resolved settings here to bridge the file→env gap and to ensure the probe
    validates exactly what was persisted (not a stale inherited env value).
    """
    from rebar._engine import engine_dir, engine_env

    script = str(engine_dir() / "jira-capability-probe.py")
    env = engine_env()
    if extra_env:
        env = {**env, **extra_env}
    return subprocess.call([sys.executable, script, *argv], env=env)


def _grounding_info(argv: list[str]) -> int:
    """``rebar grounding-info`` → the static code-grounding oracle contract.

    Repo-independent (no store, no auto-init). The ``report`` profile: a human
    summary by default, the ``grounding_info`` schema under ``--output json``.
    """
    import json as _json

    import rebar
    from rebar._engine_support.output import OutputFormatError, parse_output

    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    if rest:
        sys.stderr.write("Usage: rebar grounding-info [--output json]\n")
        return 1

    info = rebar.grounding_info()
    if fmt == "json":
        sys.stdout.write(_json.dumps(info, ensure_ascii=False) + "\n")
        return 0

    lines = [
        f"code-grounding oracle contract (dimensions v{info['dimensions_version']})",
        f"  dimensions:      {', '.join(info['dimensions'])}",
        f"  reference kinds: {', '.join(info['reference_kinds'])}",
        f"  abstain reasons: {', '.join(info['abstain_reasons'])}",
        f"  outcomes:        {', '.join(info['outcomes'])}",
        f"  jobs:            {', '.join(info['jobs'])}",
        f"  tiers:           {', '.join(info['provenance_tiers'])}",
        "  backends:",
    ]
    for b in info["backends"]:
        mark = "available" if b["available"] else "unavailable"
        ver = f" {b['version']}" if b.get("version") else ""
        lines.append(f"    - {b['name']}: {mark}{ver}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


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
    if sub in _IO:
        from rebar._io import _cli as _io_cli

        if sub == "import":
            ensure_initialized(init_only=False)
            return _io_cli.import_cli(rest)
        ensure_initialized(init_only=True)
        return _io_cli.export_cli(rest)
    if sub == "fsck":
        ensure_initialized(init_only=False)
        from rebar._commands import fsck as _fsck

        return _fsck.fsck_cli(rest)
    if sub == "fsck-recover":
        # The recover path resolves its own tracker (honors REBAR_TRACKER_DIR /
        # --tracker-dir); the dispatcher only auto-inits when
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
    if sub == "grounding-info":
        # Repo-INDEPENDENT static read (no store, no auto-init): the code-grounding
        # oracle integration contract. Owns its own --output parsing (report profile).
        return _grounding_info(rest)
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
    # Observability floor: install a stderr handler on the ``rebar`` root logger so
    # swallowed failures surface as diagnostics. Never stdout — CLI *data*
    # ``print(json.dumps(...))`` is a machine contract. See ``rebar._logging``.
    from rebar._logging import install_stderr_handler

    install_stderr_handler("rebar")

    argv = list(sys.argv[1:] if argv is None else argv)

    # A REMOVED, still-set, load-bearing input (env var / TOML key / legacy file) raises
    # RemovedInputError (a BaseException) from anywhere in the dispatch body below. Catch
    # it at this single boundary so the command surfaces the targeted migration message +
    # a non-zero exit, NOT a raw traceback. (BaseException would otherwise print one.)
    from rebar._deprecations import RemovedInputError

    try:
        return _main_dispatch(argv)
    except RemovedInputError as e:
        sys.stderr.write(str(e) + "\n")
        return 1


def _main_dispatch(argv: list[str]) -> int:
    """The full CLI dispatch body: the ``-c`` override parse, every in-process
    intercept (reconcile/review/…/identity/config/audit), and ``return _dispatch(...)``.
    Wrapped by :func:`main` in a ``RemovedInputError`` handler (see there)."""
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

    if argv and argv[0] == "verify-completion":
        return _verify_completion(argv[1:])

    # review-plan intercept (native rebar.llm plan-review gate; owns its --help).
    if argv and argv[0] == "review-plan":
        return _review_plan(argv[1:])

    # sign-review intercept (cheap re-sign of a plan-review attestation from the last
    # REVIEW_RESULT sidecar; NO LLM. Owns its --help like review-plan).
    if argv and argv[0] == "sign-review":
        return _sign_review(argv[1:])

    # enrich intercept (cross-ticket overlap drain + status; native rebar.llm, epic
    # only-crave-art). `rebar enrich [--drain|--once|status]`.
    if argv and argv[0] == "enrich":
        from rebar import config as _enrich_config
        from rebar.llm.enrich_drain import cmd_enrich

        return cmd_enrich(argv[1:], str(_enrich_config.tracker_dir()))

    # explain intercept (WS10: `rebar explain <criterion-id>` — a pure registry/guide READ, no
    # LLM; owns its --help like review-plan, so no help/*.txt or dispatch arm).
    if argv and argv[0] == "explain":
        return _explain(argv[1:])

    # verify-commit-ticket intercept (commit-message ticket gate; owns its --help). A pure
    # intercept like review-plan: no help/*.txt, no dispatch arm, no golden capture.
    if argv and argv[0] == "verify-commit-ticket":
        from rebar._commands import verify_commit

        return verify_commit.cli(argv[1:])

    # verify-identity intercept (authenticated-authorship merge-gate; owns its --help). A
    # pure intercept like verify-commit-ticket: no help/*.txt, no dispatch arm. `verify-identity`
    # is the canonical name; `verify-authorship` is a back-compat alias (both dispatch here).
    # BOTH use the equality-test form so the gen_cli_reference drift regex detects them and the
    # curated CLI reference documents the command + its alias (epic gnu-whale-ichor / AC7).
    if argv and argv[0] == "verify-identity":
        from rebar._commands import verify_authorship

        return verify_authorship.cli(argv[1:])
    if argv and argv[0] == "verify-authorship":  # back-compat alias for verify-identity
        from rebar._commands import verify_authorship

        return verify_authorship.cli(argv[1:])
    if argv and argv[0] == "verify-opcert":  # required-environment op-cert merge-gate (story 4214)
        from rebar._commands import verify_opcert

        return verify_opcert.cli(argv[1:])
    if argv and argv[0] == "trusted-env":  # maintain .rebar/trusted_environments.yaml (story 4214)
        from rebar._commands import trusted_env_cmd

        return trusted_env_cmd.cli(argv[1:])
    if argv and argv[0] == "remote-cert":  # trusted op-cert gate service client (story ee0b)
        from rebar._commands import remote_cert

        return remote_cert.cli(argv[1:])

    # workflow intercept (native rebar.llm.workflow DSL toolchain; owns its --help).
    if argv and argv[0] == "workflow":
        return _workflow(argv[1:])

    # llm intercept (the LLM-framework setup wizard; owns its --help).
    if argv and argv[0] == "llm":
        return _llm(argv[1:])

    # jira-onboard intercept (the interactive Jira onboarding wizard; owns its
    # --help, like llm/reconcile). Detects + prompts + persists + validates.
    if argv and argv[0] == "jira-onboard":
        from rebar._cli._jira_onboard import jira_onboard

        return jira_onboard(argv[1:])

    # prompt intercept (prompt evals — WS-G; owns its --help).
    if argv and argv[0] == "prompt":
        return _prompt(argv[1:])

    # criteria intercept (per-criterion calibration eval — story 55b8; owns its --help).
    if argv and argv[0] == "criteria":
        return _criteria(argv[1:])

    # identity intercept (the identity entity: create + self-pointer; owns its own
    # --help like reconcile/review). Full auto-init (it composes a CREATE write).
    if argv and argv[0] == "identity":
        rest = argv[1:]
        if not rest or rest[0] not in ("--help", "-h", "help"):
            ensure_initialized(init_only=False)
        from rebar._commands import identity as _identity

        return _identity.identity_cli(rest)

    # audit intercept (native audit read-layer aggregator; owns its own --help, like
    # reconcile/review). `audit` HAS pinned help text (help/audit.txt registers it as a
    # known subcommand), so `rebar audit --help` / `rebar help audit` fall through to the
    # shared help machinery below; only an actual invocation (`rebar audit show …`) is
    # intercepted here, so both help forms render the SAME pinned text byte-for-byte.
    if argv and argv[0] == "audit" and not (len(argv) >= 2 and argv[1] in ("--help", "-h")):
        from rebar._cli._audit_commands import audit_cli

        return audit_cli(argv[1:])

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


if __name__ == "__main__":
    sys.exit(main())
