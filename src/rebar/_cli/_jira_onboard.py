"""Implement interactive Jira onboarding for ``rebar bridge setup``.

Resolve non-secret coordinates with the reconciler's environment-over-file
precedence, prompt for missing values, and persist them to owned ``rebar.toml``.
``JIRA_API_TOKEN`` remains environment-only. Validation invokes the Jira capability
probe with the persisted settings, without importing the subprocess-scoped engine.
"""

from __future__ import annotations

import sys

from rebar._cli._parser import guard_parse_errors
from rebar._cli._parsers.advanced import jira as _jira_parsers

# The env var the secret token lives in — NEVER persisted to a config file.
_TOKEN_ENV = "JIRA_API_TOKEN"


class _Detected:
    """The resolved (non-secret) Jira coordinates + whether the secret token is set."""

    __slots__ = ("project", "token_present", "url", "user")

    def __init__(self, url: str, user: str, project: str, token_present: bool) -> None:
        self.url, self.user, self.project, self.token_present = url, user, project, token_present


def _detect() -> _Detected:
    """Resolve Jira coordinates and token presence through the owned config seam.

    Environment values override file values, and the token remains environment-only.
    Malformed configuration degrades to environment-only detection so the wizard can
    repair it. HTTPS enforcement remains on write and reconciliation paths.
    """
    from rebar import config

    url, user, project, token_present = config.resolve_jira_detection()
    return _Detected(url, user, project, token_present)


def _prompt_value(label: str, current: str) -> str:
    """Prompt until a required value is supplied.

    Enter accepts an existing default. EOF or interruption propagates so the caller
    can abort before writing.
    """
    suffix = f" [{current}]" if current else ""
    while True:
        raw = input(f"{label}{suffix}: ").strip()
        if raw:
            return raw
        if current:
            return current
        sys.stdout.write(f"  {label} is required.\n")


def _detected_line(name: str, value: str) -> str:
    return f"  {name:<8} {'= ' + value if value else '(missing)'}\n"


@guard_parse_errors
def jira_onboard(argv: list[str], *, prog: str = "rebar bridge setup") -> int:
    """Run the Jira setup wizard under its entrypoint-specific program name."""
    parser = _jira_parsers.build(prog=prog)
    args = parser.parse_args(argv)

    from rebar import config as _config

    # --reset: clear-and-exit (confirm unless --yes); never re-prompts inline.
    if args.reset:
        if not args.yes:
            try:
                ans = input("Clear persisted Jira url/user/project from rebar.toml? [y/N]: ")
            except (EOFError, KeyboardInterrupt):
                sys.stdout.write("\nAborted.\n")
                return 1
            if ans.strip().lower() not in ("y", "yes"):
                sys.stdout.write("Aborted; nothing changed.\n")
                return 1
        try:
            target = _config.write_jira_config(clear=True)
        except _config.ConfigError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1
        _config.reset_config_cache()
        sys.stdout.write(f"Cleared Jira settings in {target}.\n")
        return 0

    # Detect current settings (env > file precedence; the token is read env-only).
    current = _detect()
    sys.stdout.write("rebar Jira onboarding\n\nDetected settings:\n")
    sys.stdout.write(_detected_line("url", current.url))
    sys.stdout.write(_detected_line("user", current.user))
    sys.stdout.write(_detected_line("project", current.project))
    token_present = current.token_present
    token_state = f"set (env {_TOKEN_ENV})" if token_present else "(missing — env only)"
    sys.stdout.write(f"  token    {token_state}\n\n")

    # CLI values win. Otherwise prompt with detected defaults. Gather every coordinate
    # before writing so EOF or interruption cannot leave partial configuration.
    non_interactive = any(v is not None for v in (args.url, args.user, args.project))
    try:
        if non_interactive:
            url = args.url if args.url is not None else current.url
            user = args.user if args.user is not None else current.user
            project = args.project if args.project is not None else current.project
        else:
            url = _prompt_value("Jira URL", current.url)
            user = _prompt_value("Jira user (email)", current.user)
            project = _prompt_value("Default project key", current.project)
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write("\nAborted; nothing written.\n")
        return 1

    # Persist the three NON-SECRET values; the token is never written.
    try:
        target = _config.write_jira_config(url, user, project)
    except _config.ConfigError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    _config.reset_config_cache()
    created_new = target.name == "rebar.toml"
    sys.stdout.write(f"\nWrote Jira url/user/project to {target}.\n")
    sys.stdout.write(
        f"  Note: the secret {_TOKEN_ENV} is NEVER written to a config file — keep it\n"
        f"  in your environment (e.g. `export {_TOKEN_ENV}=...`).\n"
    )
    if created_new:
        sys.stdout.write(
            f"  (Created {target.name}; to revert to pyproject.toml-based config, "
            f"delete {target}.)\n"
        )

    # Validate end-to-end via bridge check-access, with the persisted settings injected
    # into the probe's environment (it reads JIRA_* from os.environ, not the config).
    if args.no_validate:
        sys.stdout.write(
            "\nSkipped validation (--no-validate). Run `rebar bridge check-access` to verify.\n"
        )
        return 0
    if not token_present:
        sys.stdout.write(
            f"\n{_TOKEN_ENV} is not set, so the live bridge check-access is skipped.\n"
            f"  Export {_TOKEN_ENV}, then run `rebar bridge check-access` to validate.\n"
        )
        return 0

    sys.stdout.write("\nValidating with bridge check-access...\n")
    from rebar._cli import _bridge_probe

    extra_env = {"JIRA_URL": url, "JIRA_USER": user, "JIRA_PROJECT": project}
    return _bridge_probe([], extra_env=extra_env)
