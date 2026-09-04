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
"""

from __future__ import annotations

import subprocess
import sys

from rebar._cli import _help, _help_route, _registry
from rebar._cli._init import ensure_initialized, ensure_store_mounted_best_effort
from rebar._mcp_errors import js_safe_dumps

# The registry route table is the SOLE routing authority; the router derives the four
# policy sets it still consults at runtime from it (RP-05 S6 cutover). ``_registry``
# imports only stdlib, so this stays cheap and pulls in no command handlers.
_DERIVED_POLICY_SETS = _registry.derive_policy_sets()
# EXCLUDED from the central best-effort store-mount gate (bug ad9f): `init` IS init (it
# must not pre-mount) and `scratch` is filesystem-only. ``config validate`` is also
# excluded in ``_store_mount_eligible``: its job is to aggregate bad config, so consulting
# config to locate a store would fail before that audit runs. Everything else passes
# through the gate, which silently no-ops when there is no attachable store.
_NO_AUTO_MOUNT = _DERIVED_POLICY_SETS["_NO_AUTO_MOUNT"]
# Every pure-intercept subcommand the router routes through the executor. The central
# store mount (bug ad9f) must keep firing for these — several touch the store yet return
# before any per-arm ensure_initialized. test_central_mount_gate_ad9f pins the
# store-touching-intercept behavior.
_INTERCEPTS = _DERIVED_POLICY_SETS["_INTERCEPTS"]


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


def _wants_help(rest: list[str]) -> bool:
    """Deprecated shim — see :func:`rebar._cli._help_route.wants_help`."""
    return _help_route.wants_help(rest)


def _help_requested(sub: str, rest: list[str]) -> bool:
    """Deprecated shim — see :func:`rebar._cli._help_route.help_requested`."""
    return _help_route.help_requested(sub, rest)


def _enrich(rest: list[str]) -> int:
    """``rebar enrich`` handler — the enrich drain/status intercept."""
    from rebar import config as _config
    from rebar.llm.enrich_drain import cmd_enrich

    return cmd_enrich(rest, str(_config.tracker_dir()))


def _identity_intercept(rest: list[str]) -> int:
    """Initialize identity commands except help requests, then dispatch.

    The bare form and command invocations initialize the store. Top-level and child help
    requests do not initialize or mount it.
    """
    help_form = bool(rest) and (rest[0] == "help" or _help_route.wants_help(rest))
    if not rest or not help_form:
        ensure_initialized(init_only=False)
    from rebar._commands import identity as _identity

    return _identity.identity_cli(rest)


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


def _bridge_suggest_mapping(argv: list[str]) -> int:
    """``rebar bridge suggest-mapping <PROJECT> [--write]`` → the read-only mapping probe.

    Observes a live Jira project through the read-only probe port
    (``rebar_reconciler.mapping_probe``) and emits a suggested ``[mapping.projects.<KEY>]``
    block seeded with the project's real vocabulary and identity-seed axis maps. Reads
    only — the port has no create/transition/delete surface, so this never mutates Jira
    (distinct from ``check-access``). Default: serialize the block to stdout. ``--write``:
    deep-merge it into a rebar-owned ``rebar.toml`` (existing keys win — hand edits are
    never clobbered).

    The port is obtained through ``mapping_probe.build_probe`` as a MODULE ATTRIBUTE so a
    test monkeypatch on that factory injects an offline fake."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="rebar bridge suggest-mapping",
        description="Inspect a live Jira project (read-only) and suggest a [mapping] section.",
    )
    parser.add_argument("project", metavar="PROJECT", help="The Jira project key to inspect.")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Deep-merge the suggestion into a rebar-owned rebar.toml (existing keys win).",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else int(exc.code)

    # The engine ships under ``rebar/_engine``; put it on the in-process import path so
    # ``rebar_reconciler.mapping_probe`` resolves as the SAME module object a test patches.
    from rebar._engine import engine_dir

    eng = str(engine_dir())
    if eng not in sys.path:
        sys.path.insert(0, eng)
    import rebar_reconciler.mapping_probe as mapping_probe

    key = args.project
    try:
        port = mapping_probe.build_probe()
        block = mapping_probe.build_mapping_layer(port, key)
    except Exception as exc:  # noqa: BLE001 - surface any probe failure as a clean UX error
        sys.stderr.write(
            f"Error: could not probe Jira project {key!r}: {exc}\n"
            "Check the project key exists, JIRA_URL/JIRA_USER/JIRA_PAT are set, and you "
            "have access.\n"
        )
        return 1

    if args.write:
        return _suggest_mapping_write(key, block)

    from rebar._config_writer import _emit_config_toml

    sys.stdout.write(_emit_config_toml({"mapping": block}))
    return 0


def _suggest_mapping_deep_merge(incoming: dict, existing: dict) -> dict:
    """Deep-merge ``existing`` over ``incoming`` so EXISTING keys win at every level — a
    hand-edited mapping value is never clobbered by the fresh suggestion."""
    out = dict(incoming)
    for k, v in existing.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _suggest_mapping_deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _suggest_mapping_write(key: str, block: dict) -> int:
    """Deep-merge the suggested ``block`` into a rebar-owned ``rebar.toml`` and write it
    atomically. Target selection mirrors ``write_jira_config``: an existing ``rebar.toml``
    is the target, else ``<repo_root>/rebar.toml`` is created; a user ``pyproject.toml`` is
    NEVER edited. The whole file is read with stdlib ``tomllib``, mutated in memory, and
    re-emitted via ``_emit_config_toml`` (existing keys win under
    ``mapping.projects.<KEY>``), preserving flat ``[jira]``/``[tracker]`` siblings."""
    import tomllib

    from rebar import config as _config
    from rebar._config_schema import ConfigError
    from rebar._config_writer import _emit_config_toml
    from rebar._store.fsutil import atomic_write

    base = _config.repo_root()
    proj = _config._discover_project_config()
    target = proj[0] if (proj is not None and proj[1] == "toml") else base / "rebar.toml"

    data: dict = {}
    if target.is_file():
        try:
            data = tomllib.loads(target.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            sys.stderr.write(f"Error: cannot read existing config {target}: {exc}\n")
            return 1

    mapping = data.get("mapping")
    if not isinstance(mapping, dict):
        mapping = {}
    projects = mapping.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    incoming_layer = block.get("projects", {}).get(key, {})
    existing_layer = projects.get(key)
    if isinstance(existing_layer, dict):
        projects[key] = _suggest_mapping_deep_merge(incoming_layer, existing_layer)
    else:
        projects[key] = incoming_layer
    mapping["projects"] = projects
    data["mapping"] = mapping

    try:
        text = _emit_config_toml(data)
    except ConfigError as exc:
        sys.stderr.write(f"Error: cannot serialize config: {exc}\n")
        return 1
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Unique same-dir temp (mkstemp) + os.replace — a target-derived `rebar.toml.tmp`
        # is shared by every concurrent writer, so one of them is silently lost.
        atomic_write(target, text, encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"Error: could not write config {target}: {exc}\n")
        return 1
    sys.stdout.write(f"Wrote suggested [mapping.projects.{key}] to {target}\n")
    return 0


def _grounding_info(argv: list[str]) -> int:
    """``rebar grounding-info`` → the static code-grounding oracle contract.

    Repo-independent (no store, no auto-init). The ``report`` profile: a human
    summary by default, the ``grounding_info`` schema under ``--output json``.
    """
    import rebar
    from rebar._engine_support.output import OutputFormatError, parse_output

    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    from rebar._cli._parsers.core.grounding import build as build_grounding

    # Parser of record for grounding-info's accepted grammar; the surplus-positional
    # ``Usage:`` guard below is retained as the bespoke reject.
    build_grounding(prog="rebar grounding-info").parse_known_args(rest)
    if rest:
        sys.stderr.write("Usage: rebar grounding-info [--output json]\n")
        return 1

    info = rebar.grounding_info()
    if fmt == "json":
        sys.stdout.write(js_safe_dumps(info, ensure_ascii=False) + "\n")
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
    """Deprecated shim — see :func:`rebar._cli._help_route.emit_subcommand_help`."""
    return _help_route.emit_subcommand_help(sub)


# Mutating verbs that confirm their result on stdout (ticket 6bda-9d58-8546-4638).
# The global --quiet / --output flags are pre-extracted here at the router — BEFORE
# any partition dispatch — because the positional-only _REGISTRY leaves
# (rebar._commands.__init__) usage-error on any option-looking token, so a
# per-verb parse could never see them. Both sets are derived from the registry route
# table (RP-05 S6): ``_CONFIRM_SCOPE`` is consulted by ``_dispatch``, ``_LEGACY_OUTPUT``
# by ``_dispatch_confirmable`` (verbs that parsed --output themselves before the global
# extraction existed — the extracted format is re-injected so their own parse_output
# keeps producing the byte-identical, pre-existing JSON shapes).
_CONFIRM_SCOPE = _DERIVED_POLICY_SETS["_CONFIRM_SCOPE"]
_LEGACY_OUTPUT = _DERIVED_POLICY_SETS["_LEGACY_OUTPUT"]


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
    """The core dispatch proper (post any global-flag extraction).

    The selected registry route is the single execution authority: it names the
    lazy handler, its bounded adapter call shape, and its init policy (RP-05 S3).
    """
    from rebar._cli import _execute

    return _execute.execute(sub, rest)


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
    from rebar.config import ConfigError

    try:
        return _main_dispatch(argv)
    except RemovedInputError as e:
        sys.stderr.write(str(e) + "\n")
        return 1
    except ConfigError as e:
        # An unreadable/invalid config is a LOUD, clean operator-facing error, never a
        # traceback (operator ruling 39f8-ae7c: gated operations raise ConfigError on an
        # unreadable config; this boundary renders it the same way the `-c` override
        # parse below already renders its own ConfigError).
        sys.stderr.write(f"Error: {e}\n")
        return 1
    except TrackerRootError as e:
        # bug 176d: the read core now RAISES instead of calling sys.exit, so the exit
        # decision lives here. The auto-init middleware (_cli/_init.py) already refuses
        # a non-repo cwd before dispatch, so this is the residual path — an explicit
        # non-repo root, or the repo vanishing mid-session. Same line, same exit 1.
        sys.stderr.write(f"Error: {e}\n")
        return 1


def _main_dispatch(argv: list[str]) -> int:
    """The full CLI dispatch body: the ``-c`` override parse, composing-and-binding the
    operation snapshot, the central store-mount gate, and ``return _dispatch(...)``
    (which routes every command, intercepts included, through the registry executor).
    Wrapped by :func:`main` in a ``RemovedInputError`` handler (see there)."""
    # Canonical help / overview / unknown pre-scan (RP-05 S2d): answer a help, bare-overview,
    # or unknown-subcommand request from the committed help artifacts BEFORE any operation
    # snapshot, config materialization, store mount, handler/factory resolution, or optional
    # import. An exit code means the request was served. None means a command invocation or a
    # nested child help request continues to dispatch.
    _served = _help_route.pre_scan(argv)
    if _served is not None:
        return _served

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

    # Authoritative operation snapshot (RP-04 S2, ticket 3a08): compose ONE snapshot per
    # invocation, after config is resolvable but before any dispatch/store-mount/write
    # effect, and BIND it active for the whole invocation — store/config helpers
    # (rebar.config.tracker_dir / tickets_branch / tickets_remote) consult it instead of
    # a fresh ambient read, so a later env/project/CWD mutation cannot change this
    # operation's selected tracker dir/branch/remote mid-flight. Fails OPEN on a
    # malformed/insecure config (matching the prior shadow's discipline): some legacy
    # operations (e.g. ``bridge setup --reset``, which only clears a bad section) must
    # keep working even when composition itself cannot succeed — with no snapshot bound,
    # downstream helpers fall back to their pre-existing ambient resolution and a real
    # operation that NEEDS a valid config still fails on its own read, same as before.
    from rebar._operation_config import compose_and_bind_operation_snapshot

    with compose_and_bind_operation_snapshot():
        # Central store-mount gate (bug ad9f): every store-touching command — INCLUDING the
        # pure intercepts (verify-commit-ticket, ...) that historically bypassed the per-arm
        # ensure_initialized — mounts the store ONCE here, before dispatch, so none can
        # silently skip it. Best-effort (attach-if-possible, never error, never first-time-init,
        # never reconverge): the strict per-arm ensure_initialized calls remain and still own
        # greenfield refusal + reconverge for store-REQUIRING arms. Excluded: no-store commands,
        # help/usage rendering, and unknown subcommands (bug dd62 — see _store_mount_eligible).
        if _store_mount_eligible(argv):
            ensure_store_mounted_best_effort()

        # Help, overview, and unknown-subcommand forms were already served by
        # ``_help_route.pre_scan`` at the top of this function (before any snapshot/config/mount).
        # Reaching here means a real command invocation — including the intercept commands,
        # which now carry registry execution metadata and route through the executor like every
        # other command (RP-05 S6); route it to the dispatcher.
        sub, rest = argv[0], argv[1:]
        return _dispatch(sub, rest)


if __name__ == "__main__":
    sys.exit(main())
