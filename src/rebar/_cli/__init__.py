"""Provide the in-process argparse entrypoint for ``rebar``.

``main()`` tokenizes the subcommand. Implementations parse their own flags. Pinned
package data supplies help, overview, and unknown-command output. Registry routes
dispatch reads and writes with their per-command initialization policy.
"""

from __future__ import annotations

import subprocess
import sys

from rebar._cli import _help, _help_route, _registry
from rebar._cli._init import ensure_initialized, ensure_store_mounted_best_effort
from rebar._mcp_errors import js_safe_dumps

# The stdlib-only registry is the sole routing authority and source of runtime policy sets.
_DERIVED_POLICY_SETS = _registry.derive_policy_sets()
# ``init``, ``scratch``, and ``config validate`` bypass the central mount gate. The last
# must report invalid configuration before consulting it. Other commands mount only when
# an attachable store exists.
_NO_AUTO_MOUNT = _DERIVED_POLICY_SETS["_NO_AUTO_MOUNT"]
# Pure intercepts still pass through the central mount gate because some access the store
# before any per-arm initialization.
_INTERCEPTS = _DERIVED_POLICY_SETS["_INTERCEPTS"]


def _store_mount_eligible(argv: list[str]) -> bool:
    """Return whether this invocation should attempt the central store mount.

    Empty input, ``init``, ``scratch``, ``config validate``, help forms, and unknown
    commands are ineligible. Help remains side-effect free. Strict per-arm initialization
    still mounts or rejects greenfield stores for commands that require one.
    """
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
    """Run Jira's capability probe without initializing a local tracker.

    The probe inherits output streams and runs under ``sys.executable`` with
    ``engine_env()``. ``extra_env`` overrides that environment, allowing setup to test
    the newly persisted ``JIRA_URL``, ``JIRA_USER``, and ``JIRA_PROJECT`` values. The
    probe creates and deletes only its throwaway Jira issue.
    """
    from rebar._engine import engine_dir, engine_env

    script = str(engine_dir() / "jira-capability-probe.py")
    env = engine_env()
    if extra_env:
        env = {**env, **extra_env}
    return subprocess.call([sys.executable, script, *argv], env=env)


def _bridge_suggest_mapping(argv: list[str]) -> int:
    """Probe a Jira project read-only and emit or persist a mapping suggestion.

    The probe exposes no Jira mutation operations. Its project vocabulary and identity
    axes seed a mapping written to stdout. ``--write`` merges it into owned
    ``rebar.toml`` data without replacing existing keys. Access the factory as a module
    attribute so tests can substitute an offline probe.
    """
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
    """Atomically merge ``block`` into an owned ``rebar.toml``.

    Use an existing ``rebar.toml`` or create one at the repository root. Never edit
    ``pyproject.toml``. Existing ``mapping.projects.<KEY>`` values win, while flat
    ``[jira]`` and ``[tracker]`` siblings survive reserialization.
    """
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


# Extract global confirmation flags before positional dispatch, which otherwise rejects
# option-looking tokens. Registry-derived scopes preserve each legacy command's existing
# output parsing and byte-identical JSON shape.
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

    # Catch the BaseException-derived RemovedInputError once so obsolete inputs produce the
    # migration message and exit 1 instead of a traceback.
    from rebar._deprecations import RemovedInputError
    from rebar._errors import TrackerRootError
    from rebar.config import ConfigError

    try:
        return _main_dispatch(argv)
    except RemovedInputError as e:
        sys.stderr.write(str(e) + "\n")
        return 1
    except ConfigError as e:
        # Render invalid configuration as the same clean exit-1 error used by global overrides.
        sys.stderr.write(f"Error: {e}\n")
        return 1
    except TrackerRootError as e:
        # The read core raises this residual non-repository error. The CLI owns its exit-1
        # rendering.
        sys.stderr.write(f"Error: {e}\n")
        return 1


def _main_dispatch(argv: list[str]) -> int:
    """The full CLI dispatch body: the ``-c`` override parse, composing-and-binding the
    operation snapshot, the central store-mount gate, and ``return _dispatch(...)``
    (which routes every command, intercepts included, through the registry executor).
    Wrapped by :func:`main` in a ``RemovedInputError`` handler (see there)."""
    # Serve help, the bare overview, and unknown commands from committed artifacts before
    # configuration, snapshots, mounts, or lazy imports. Other invocations continue.
    _served = _help_route.pre_scan(argv)
    if _served is not None:
        return _served

    # Leading, repeatable ``-c``/``--config`` values become the highest-precedence CLI layer
    # for every configuration consumer in this invocation.
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

    # Bind one operation snapshot after overrides and before dispatch or mounting so later
    # environment, project, or CWD changes cannot retarget the operation. Composition fails
    # open for malformed configuration and preserves legacy repair commands. Consumers that
    # require valid configuration still fail at their own boundary.
    from rebar._operation_config import compose_and_bind_operation_snapshot

    with compose_and_bind_operation_snapshot():
        # Mount an attachable store once before dispatch, including for pure intercepts. This
        # best-effort gate never initializes greenfield state or reconverges, and skips help,
        # unknown, and explicitly storeless commands. Strict per-arm initialization remains.
        if _store_mount_eligible(argv):
            ensure_store_mounted_best_effort()

        # Pre-scan already handled non-command forms. Route this invocation, including
        # registry-described intercepts, through the common dispatcher.
        sub, rest = argv[0], argv[1:]
        return _dispatch(sub, rest)


if __name__ == "__main__":
    sys.exit(main())
