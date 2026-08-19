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
from rebar._cli._init import ensure_initialized, ensure_store_mounted_best_effort
from rebar._cli._llm_commands import (
    _criteria,
    _explain,
    _llm,
    _prompt,
    _review_code,
    _review_plan,
    _scan_spec,
    _sign_review,
    _verify_completion,
)
from rebar._cli._workflow_commands import _workflow

# Commands EXCLUDED from the central best-effort store-mount gate (bug ad9f): `init`
# IS init (it must not pre-mount) and `scratch` is filesystem-only (it gets no
# init). ``config validate`` is also excluded below: its job is to aggregate bad
# config, so consulting config to locate a store would fail before that audit runs.
# Everything else passes through the gate, which silently no-ops when there is no
# attachable store, so no-store reads keep working store-less.
_NO_AUTO_MOUNT = frozenset({"init", "scratch"})
# Every pure-intercept subcommand `_main_dispatch` routes ABOVE the set-based arms. The
# central store mount (bug ad9f) must keep firing for these — several touch the store yet
# return before any per-arm ensure_initialized. Kept adjacent to the intercept ladder it
# mirrors; test_central_mount_gate_ad9f pins the store-touching-intercept behavior.
_INTERCEPTS = frozenset(
    {
        "reconcile",
        "review-code",
        "scan-spec",
        "verify-completion",
        "review-plan",
        "sign-review",
        "enrich",
        "explain",
        "verify-commit-ticket",
        "verify-identity",
        "verify-authorship",
        "verify-opcert",
        "trusted-env",
        "remote-cert",
        "workflow",
        "llm",
        "jira-onboard",
        "prompt",
        "criteria",
        "identity",
        "audit",
        "config",
        "metrics",
    }
)


def _store_mount_eligible(argv: list[str]) -> bool:
    """Should the central best-effort store mount (bug ad9f) run for this invocation?

    NOT for: an empty invocation, the no-mount arms (``init``/``scratch``), ``config
    validate`` (which must inspect invalid config without loading it first), any HELP
    rendering (``rebar help …`` / ``--help`` / ``-h`` anywhere), or an UNKNOWN
    subcommand (about to be rejected with usage). Rendering usage/help is a pure read
    of the CLI surface — creating repo state (``.tickets-tracker``) for it surprised
    every fresh worktree and clone (bug dd62 ``sapient-rutile-penguin``). Everything
    else mounts exactly as before. Skipping here can never break a store-REQUIRING arm:
    the strict per-arm ``ensure_initialized`` calls still own their own mount +
    greenfield refusal."""
    if not argv:
        return False
    sub = argv[0]
    if sub in _NO_AUTO_MOUNT or sub in ("help", "--help", "-h"):
        return False
    if sub == "config" and len(argv) > 1 and argv[1] == "validate":
        return False
    if "--help" in argv or "-h" in argv:
        return False
    return sub in _INTERCEPTS or sub in _help.known_subcommands()


# Read arms that auto-init only; the read path owns its own throttled reconverge.
_READS_INIT_ONLY = frozenset(
    {"show", "list", "next-batch", "deps", "ready", "search", "session-logs"}
)
# Read-compute arm that runs with NO auto-init (self-manages).
_READS_NO_INIT = frozenset({"validate"})
# Field-read arms that run with FULL auto-init.
_FIELD_READS = frozenset({"get-file-impact", "get-verify-commands"})
# Resolution/display arms that run with FULL auto-init.
_LOOKUPS = frozenset({"exists", "resolve", "format"})
# Graph-traversal arm that runs with FULL auto-init.
_DESCENDANTS = frozenset({"list-descendants"})
# Per-ticket gate arms that run with NO auto-init (they read
# transitively via `ticket show`, so the gate CLI itself does no auto-init).
_GATES = frozenset({"clarity-check", "check-ac", "quality-check", "summary"})
# Signature arms (native, no bash counterpart): `sign` is a write, `verify-signature`
# a read; both need an initialized store + the environment signing key.
_SIGNING = frozenset({"sign", "verify-signature"})
# Write/lifecycle arms (E3): full auto-init + reconverge before the in-process write.
_LIFECYCLE = frozenset({"transition", "reopen", "claim"})
# Compaction arms (E3): full auto-init before the in-process SNAPSHOT write.
_COMPACT = frozenset({"compact", "compact-all"})
# Bridge commands share the explicit routing census, while retaining their own
# initialization policies below.
_BRIDGE = frozenset({"bridge", "bridge-status", "bridge-fsck"})
_HIDDEN_ALIASES = frozenset({"bridge-status"})


def _wants_help(rest: list[str]) -> bool:
    """True if a bare ``--help``/``-h`` appears before any ``--`` terminator.

    The dispatcher must honour a help flag in ANY position, not only ``rest[0]``:
    ``rebar create task --help`` used to fall through to the create handler, which
    consumed ``--help`` as the positional title and created a placeholder ticket
    (bug b8de).

    Scanning stops at the first ``--`` so a caller can suppress the help intercept
    at the dispatcher. This is a dispatcher-level convention only: downstream write
    handlers parse their own positionals and do not themselves treat ``--`` as an
    argument terminator, so ``--`` is an escape from help interception, not a
    general "everything after is literal data" contract. A ``--help``/``-h`` meant
    as the *value* of a value-taking option (e.g. ``-d --help``) is likewise read
    as a help request here; passing such a literal is an unsupported edge, matching
    argparse's own precedence of the help flag.
    """
    for tok in rest:
        if tok == "--":
            return False
        if tok in ("--help", "-h"):
            return True
    return False


def _help_requested(sub: str, rest: list[str]) -> bool:
    """Whether ``rebar <sub> …`` is a help request the DISPATCHER should serve itself.

    Nested-dispatch families (the ``bridge`` group) own their children's help, so
    for them only a LEADING ``--help``/``-h`` asks for the family's own usage — a
    later flag belongs to a child (``bridge preview --help`` → preview usage, served
    by ``bridge_cli``, not the dispatcher). This keeps ``bridge --help`` identical to
    ``help bridge``. Every other command has no nested help, so a ``--help``/``-h`` in
    any position before a ``--`` is a usage request for it (bug b8de).
    """
    if sub in _BRIDGE:
        return bool(rest) and rest[0] in ("--help", "-h")
    return _wants_help(rest)


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
        "attach-commits",
        "session-log",
    }
)

# Keep the top-level router simple enough to be stable across the Python-version
# specific ASTs Ruff scans in CI. Each partition follows an existing dispatch
# seam and retains the established per-command initialization policy.
_DISPATCH_PRIMARY = (
    frozenset({"init", "scratch", "metrics", "delete"})
    | _READS_INIT_ONLY
    | _READS_NO_INIT
    | _LIFECYCLE
    | _COMPACT
    | _BRIDGE
)
_DISPATCH_MIDDLE = (
    _IO | frozenset({"doctor", "fsck", "fsck-recover", "tracker-maintenance"}) | _WRITES_FULL
)


def _reconcile(argv: list[str]) -> int:
    """Compatibility wrapper for the established ``rebar reconcile`` spelling."""
    from rebar._cli._bridge_commands import launch_reconciler

    return launch_reconciler(argv)


def _bridge_probe(argv: list[str], *, extra_env: dict[str, str] | None = None) -> int:
    """``rebar bridge check-access`` → live Jira capability preflight.

    Launches the genuine python probe (``jira-capability-probe.py``) under
    ``sys.executable`` with ``engine_env`` (so the engine's
    ``rebar_reconciler.adapters.jira.acli`` transport resolves). Talks only to
    Jira (creates + deletes a throwaway
    issue); needs no local tracker, so NO auto-init.
    Output streams inherit so the operator sees the PROBE_PASS/FAIL lines directly.

    ``extra_env`` overlays additional variables onto ``engine_env()`` before launch,
    and these **override** any same-named variable inherited from ``os.environ``
    (``{**engine_env(), **extra_env}`` — last writer wins). The probe reads
    ``JIRA_URL`` / ``JIRA_USER`` / ``JIRA_PROJECT`` from its process env (not from
    ``load_config()``), so ``rebar bridge setup`` passes the just-persisted,
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
    """Print ``sub``'s usage.

    Known subcommand → stdout, exit 0. Unknown → error + blank + overview all to
    stderr, exit 1.
    """
    text = _help.subcommand_help(sub)
    if text is not None:
        sys.stdout.write(text)
        return 0
    sys.stderr.write(f"Error: unknown subcommand '{sub}'\n\n")
    sys.stderr.write(_help.overview())
    return 1


def _dispatch_bridge(sub: str, rest: list[str]) -> int:
    """Dispatch bridge commands while preserving their distinct init policies."""
    if sub in {"bridge", "bridge-status"}:
        from rebar._cli._bridge_commands import bridge_cli

        return bridge_cli(rest if sub == "bridge" else ["status", *rest])
    from rebar._cli._bridge_commands import bridge_fsck_cli

    return bridge_fsck_cli(rest)


def _dispatch_primary(sub: str, rest: list[str]) -> int:
    """Route bootstrap, read, lifecycle, compaction, and bridge commands."""
    if sub == "init":
        # Explicit bootstrap — NEVER triggers auto-init (it IS init).
        from rebar._commands import init as _init_cmd

        return _init_cmd.init_cli(rest)
    if sub == "scratch":
        # Filesystem-only per-ticket store — NO auto-init.
        from rebar._commands import scratch

        return scratch.scratch_cli(rest)
    if sub == "metrics":
        # Read command over the declarative metric registry. Init-only like the
        # other reads: it resolves the store root but composes no writes.
        ensure_initialized(init_only=True)
        from rebar._commands import metrics as _metrics

        return _metrics.metrics_cli(rest)
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
        return _dispatch_bridge(sub, rest)
    raise RuntimeError(f"rebar: primary subcommand {sub!r} has no in-process handler")


def _dispatch_middle(sub: str, rest: list[str]) -> int:
    """Route import/export, repair, and ordinary write commands."""
    if sub in _IO:
        from rebar._io import _cli as _io_cli

        if sub == "import":
            ensure_initialized(init_only=False)
            return _io_cli.import_cli(rest)
        ensure_initialized(init_only=True)
        return _io_cli.export_cli(rest)
    if sub == "doctor":
        # Read-only by default, so it must NOT join _WRITES_FULL — that arm
        # reconverges the store on EVERY invocation. Only --repair writes, and only
        # it needs the full init (same conditional shape as the import/export arm).
        ensure_initialized(init_only="--repair" not in rest)
        from rebar._commands import doctor as _doctor

        return _doctor.doctor_cli(rest)
    if sub == "fsck":
        ensure_initialized(init_only=False)
        from rebar._commands import fsck as _fsck

        return _fsck.fsck_cli(rest)
    if sub == "tracker-maintenance":
        # The SUPPORTED door for raw git in the tracker (bug 2fa6). Needs a real store to
        # inspect, and must never auto-init one — an operator reaching for maintenance is
        # repairing an EXISTING store, and silently creating a fresh one would hide that.
        ensure_initialized(init_only=False)
        from rebar._commands import tracker_maintenance as _tracker_maintenance

        return _tracker_maintenance.tracker_maintenance_cli(rest)
    if sub == "fsck-recover":
        # The recover path resolves its own tracker (honors REBAR_TRACKER_DIR /
        # --tracker-dir); it only auto-inits when
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
    raise RuntimeError(f"rebar: middle subcommand {sub!r} has no in-process handler")


def _dispatch_suffix(sub: str, rest: list[str]) -> int:
    """Route field, graph, gate, signing, and static-read commands."""
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


# Mutating verbs that confirm their result on stdout (ticket 6bda-9d58-8546-4638).
# The global --quiet / --output flags are pre-extracted here at the router — BEFORE
# any partition dispatch — because the positional-only _REGISTRY leaves
# (rebar._commands.__init__) usage-error on any option-looking token, so a
# per-verb parse could never see them.
_CONFIRM_SCOPE = _WRITES_FULL | _LIFECYCLE
# Verbs that parsed --output themselves before the global extraction existed. The
# extracted format is re-injected so their own parse_output keeps producing the
# byte-identical, pre-existing JSON shapes (their parsers strip the flag again
# before any positional handling, so the position of the re-injection is inert).
_LEGACY_OUTPUT = frozenset({"create", "idea", "transition", "claim", "reopen"})


def _dispatch_confirmable(sub: str, rest: list[str]) -> int:
    """Pre-extract the global output flags for a mutating verb, then dispatch.

    Extraction is position-independent but never consumes tokens after ``--``
    (see :func:`rebar._commands._confirm.extract_global_flags`); the result is
    installed as the per-invocation confirmation context every confirmation
    emit consults."""
    from rebar._commands._confirm import (
        OutputFormatError,
        confirmation_context,
        extract_global_flags,
    )

    try:
        rest, quiet, fmt = extract_global_flags(rest)
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    if sub in _LEGACY_OUTPUT:
        rest = [*rest, "--output", fmt]
    with confirmation_context(quiet=quiet, fmt=fmt):
        return _dispatch_route(sub, rest)


def _dispatch(sub: str, rest: list[str]) -> int:
    """Route a known subcommand to its in-process implementation."""
    if sub in _CONFIRM_SCOPE:
        return _dispatch_confirmable(sub, rest)
    return _dispatch_route(sub, rest)


def _dispatch_route(sub: str, rest: list[str]) -> int:
    """The partition dispatch proper (post any global-flag extraction)."""
    if sub in _DISPATCH_PRIMARY:
        return _dispatch_primary(sub, rest)
    if sub in _DISPATCH_MIDDLE:
        return _dispatch_middle(sub, rest)
    return _dispatch_suffix(sub, rest)


def main(argv: list[str] | None = None) -> int:
    """rebar CLI entry. Returns the process exit code.

    Control flow intercepts help before dispatch
    so no command is executed on a help request and the streams/exit codes
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
    from rebar._errors import TrackerRootError

    try:
        return _main_dispatch(argv)
    except RemovedInputError as e:
        sys.stderr.write(str(e) + "\n")
        return 1
    except TrackerRootError as e:
        # bug 176d: the read core now RAISES instead of calling sys.exit, so the exit
        # decision lives here. The auto-init middleware (_cli/_init.py) already refuses
        # a non-repo cwd before dispatch, so this is the residual path — an explicit
        # non-repo root, or the repo vanishing mid-session. Same line, same exit 1.
        sys.stderr.write(f"Error: {e}\n")
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

    # Shadow-mode operation snapshot (RP-04 S1): compose ONE diagnostic snapshot per
    # invocation, after config is resolvable but before any dispatch/effects. Guarded
    # and side-effect-free apart from the DEBUG diagnostic — it does NOT control
    # execution, alter output, or change exit codes.
    from rebar._operation_config import emit_shadow_snapshot

    emit_shadow_snapshot(surface="cli")

    # Central store-mount gate (bug ad9f): every store-touching command — INCLUDING the pure
    # intercepts below (verify-commit-ticket, ...) that historically bypassed the per-arm
    # ensure_initialized — mounts the store ONCE here, before dispatch, so none can silently skip
    # it. Best-effort (attach-if-possible, never error, never first-time-init, never reconverge):
    # the strict per-arm ensure_initialized calls remain and still own greenfield refusal +
    # reconverge for store-REQUIRING arms. Excluded: no-store commands, help/usage rendering,
    # and unknown subcommands (bug dd62 — see _store_mount_eligible).
    if _store_mount_eligible(argv):
        ensure_store_mounted_best_effort()

    # reconcile intercept (a native rebar op, not a per-ticket command arm).
    if argv and argv[0] == "reconcile":
        return _reconcile(argv[1:])

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

    # Compatibility jira-onboard intercept. The canonical `bridge setup` child and
    # this alias share jira_onboard; the alias retains its historical prog in help.
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

    # No subcommand: overview to stdout, exit 1.
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

    # `rebar <sub> --help|-h` → usage, no exec. For most commands a help flag in ANY
    # position before a `--` counts (else `create task --help` reaches the handler and is
    # stored as the ticket title — bug b8de); nested-dispatch families (bridge*) honour only
    # a leading flag so a child keeps its own help. Commands intercepted earlier never reach
    # here.
    if sub not in _HIDDEN_ALIASES and _help_requested(sub, rest):
        return _emit_subcommand_help(sub)

    # Unknown subcommand: error to stderr + overview to stdout, exit 1.
    if sub not in _help.known_subcommands() and sub not in _HIDDEN_ALIASES:
        sys.stderr.write(f"Error: unknown subcommand '{sub}'\n")
        sys.stdout.write(_help.overview())
        return 1

    return _dispatch(sub, rest)


if __name__ == "__main__":
    sys.exit(main())


# --- RP-05 S2a shadow-only derivation (no route cutover) --------------------
# The immutable route registry rebuilds the live policy frozensets above with
# zero delta. This derived mirror is a shadow census only: the literal
# frozensets remain the router's authoritative source, and nothing here changes
# dispatch, help, or execution. ``_registry`` imports only stdlib, so this stays
# cheap and pulls in no command handlers or optional dependencies.
from rebar._cli import _registry as _registry  # noqa: E402

_DERIVED_POLICY_SETS = _registry.derive_policy_sets()
