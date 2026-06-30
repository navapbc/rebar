"""``rebar jira-onboard`` — the interactive Jira onboarding wizard.

A native intercept (owns its own ``--help``, like ``rebar llm`` / ``reconcile``).
It DETECTS the current Jira settings the same way the reconciler's
``resolve_jira_settings`` does — ``load_config().jira.*`` with the Atlassian env
vars overriding and the token read env-only — but resolves them in-process via
:mod:`rebar.config` (the engine ``rebar_reconciler`` package is import-path-scoped
to subprocesses, so the wizard must NOT import it directly). It PROMPTS for whatever
connection coordinate is missing (``url`` / ``user`` / ``project``), PERSISTS the
three
non-secret values to a rebar-owned ``rebar.toml`` ``[jira]`` section
(:func:`rebar.config.write_jira_config`), GUIDES the operator that the secret
``JIRA_API_TOKEN`` stays an environment variable (never written to disk), and
VALIDATES end-to-end by invoking ``rebar bridge-probe`` with the resolved settings
injected into the probe's environment.

Config persistence + the reconciler read path already existed (story b5db); this
module only adds the missing interactive UX, mirroring the ``rebar llm setup``
precedent in :mod:`rebar._cli._llm_commands`.
"""

from __future__ import annotations

import argparse
import os
import sys

# The env var the secret token lives in — NEVER persisted to a config file.
_TOKEN_ENV = "JIRA_API_TOKEN"


class _Detected:
    """The resolved (non-secret) Jira coordinates + whether the secret token is set."""

    __slots__ = ("url", "user", "project", "api_token")

    def __init__(self, url: str, user: str, project: str, api_token: str) -> None:
        self.url, self.user, self.project, self.api_token = url, user, project, api_token


def _detect() -> _Detected:
    """Resolve the current Jira settings in-process, mirroring the reconciler's
    ``resolve_jira_settings`` precedence (env ``JIRA_URL/USER/PROJECT`` over
    ``load_config().jira.*``; the secret ``JIRA_API_TOKEN`` is env-only). The engine
    ``rebar_reconciler`` package is import-path-scoped to subprocesses, so we read
    the same typed config directly rather than importing it. A malformed config
    degrades to env-only (matching the resolver's fail-soft behavior)."""
    from rebar.config import ConfigError, load_config

    url = user = project = ""
    try:
        jira = load_config().jira
        url, user, project = jira.url, jira.user, jira.project
    except ConfigError:
        pass
    url = os.environ.get("JIRA_URL") or url
    user = os.environ.get("JIRA_USER") or user
    project = os.environ.get("JIRA_PROJECT") or project
    return _Detected(url, user, project, os.environ.get(_TOKEN_ENV, ""))


def _prompt_value(label: str, current: str) -> str:
    """Prompt for one required value, re-prompting on empty input.

    A pre-existing ``current`` value is offered as the default (Enter keeps it). An
    EOF (Ctrl-D) or interrupt raises ``EOFError`` / ``KeyboardInterrupt`` to the
    caller, which aborts the wizard cleanly with no partial write.
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


def jira_onboard(argv: list[str]) -> int:
    """Entry point for ``rebar jira-onboard`` (see module docstring)."""
    parser = argparse.ArgumentParser(
        prog="rebar jira-onboard",
        description=(
            "Interactively configure Jira: detect existing settings, prompt for "
            "missing url/user/project, persist them to rebar.toml, and validate via "
            "bridge-probe. The secret JIRA_API_TOKEN stays an environment variable "
            "and is never written to a config file."
        ),
    )
    parser.add_argument("--url", help="Jira base URL (non-interactive)")
    parser.add_argument("--user", help="Jira account email (non-interactive)")
    parser.add_argument("--project", help="default Jira project key (non-interactive)")
    parser.add_argument(
        "--no-validate", action="store_true", help="skip the post-onboard bridge-probe check"
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="clear the persisted [jira] url/user/project and exit (no re-prompt)",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", help="skip the --reset confirmation prompt"
    )
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
    token_present = bool(current.api_token)
    token_state = f"set (env {_TOKEN_ENV})" if token_present else "(missing — env only)"
    sys.stdout.write(f"  token    {token_state}\n\n")

    # Collect the three connection coordinates: CLI flags win; else prompt for the
    # missing ones (offering any detected value as the default). All input is
    # gathered BEFORE any write, so an EOF/Ctrl-C abort never leaves a partial file.
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

    # Validate end-to-end via bridge-probe, with the just-persisted settings injected
    # into the probe's environment (it reads JIRA_* from os.environ, not the config).
    if args.no_validate:
        sys.stdout.write(
            "\nSkipped validation (--no-validate). Run `rebar bridge-probe` to verify.\n"
        )
        return 0
    if not token_present and not os.environ.get(_TOKEN_ENV):
        sys.stdout.write(
            f"\n{_TOKEN_ENV} is not set, so the live bridge-probe check is skipped.\n"
            f"  Export {_TOKEN_ENV}, then run `rebar bridge-probe` to validate.\n"
        )
        return 0

    sys.stdout.write("\nValidating with bridge-probe...\n")
    from rebar._cli import _bridge_probe

    extra_env = {"JIRA_URL": url, "JIRA_USER": user, "JIRA_PROJECT": project}
    return _bridge_probe([], extra_env=extra_env)
