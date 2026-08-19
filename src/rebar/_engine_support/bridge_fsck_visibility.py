"""Opt-in live mapped-project visibility diagnostic for ``bridge fsck`` (ticket 9702).

READ-ONLY, ADVISORY, credential-conditional. Reuses D's single source of truth
(``access_check.check_mapped_project_visibility``) rather than hand-rolling a second
Jira existence check. Kept in a sibling module so ``bridge_fsck`` stays under the
module-size cap; ``bridge_fsck.main`` calls :func:`audit_mapped_project_visibility`
(behind ``--live-visibility``) and renders :func:`format_visibility_advisory` to
stderr, so the pinned stdout JSON contract is untouched and the exit code is never
changed.

Jira stays an OPTIONAL extra: the reconciler ``access_check`` module is imported
LAZILY, only when the diagnostic runs, so nothing here is reachable from core
``rebar doctor`` (which must stay Jira-free).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

_JIRA_CRED_KEYS = ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN")


def _load_access_check() -> ModuleType:
    """Lazily load the reconciler ``access_check`` module by its package name.

    Mirrors ``_lib_ops._engine_module``: the ``rebar_reconciler`` package lives
    under the bundled ``_engine`` dir and is loaded via ``spec_from_file_location``
    with a submodule search location, so ``access_check`` (and its ``acli`` import,
    an ``[agents]`` optional-extra) is only pulled in when the diagnostic runs.
    """
    import importlib
    import importlib.util

    package = "rebar_reconciler"
    if package not in sys.modules:
        from rebar._engine import engine_dir

        package_dir = engine_dir() / package
        spec = importlib.util.spec_from_file_location(
            package,
            package_dir / "__init__.py",
            submodule_search_locations=[str(package_dir)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
    return importlib.import_module("rebar_reconciler.access_check")


def audit_mapped_project_visibility(
    repo_root: Path,
    *,
    probe: object | None = None,
    env: dict[str, str],
    access_check_mod: ModuleType | None = None,
) -> dict:
    """Opt-in, credential-conditional, READ-ONLY, ADVISORY live visibility check.

    Reuses the single source of truth ``access_check.check_mapped_project_visibility``
    (ticket a011 / D) to answer "are every mapped key + legacy_default visible to
    the bot?". When no ``probe`` is injected AND live Jira credentials are
    absent/incomplete, the fsck layer performs its OWN ``JIRA_*`` presence check
    FIRST (mirroring ``run_access_check``'s ``missing_credentials`` contract) and
    returns an advisory ``skipped`` verdict WITHOUT building or calling the probe —
    it never hard-fails bridge fsck. Any probe error is likewise swallowed into an
    advisory verdict. Returns a JSON-serializable dict; the caller renders it to
    stderr and never lets it change bridge fsck's exit contract.
    """
    src = env
    creds_present = all(src.get(key) for key in _JIRA_CRED_KEYS)
    if probe is None and not creds_present:
        return {
            "status": "skipped",
            "reason": "missing_credentials",
            "required": [],
            "visible": [],
            "missing": [],
            "detail": (
                "no live Jira credentials (JIRA_URL / JIRA_USER / JIRA_API_TOKEN); "
                "skipping live mapped-project visibility check"
            ),
        }

    access_check = access_check_mod if access_check_mod is not None else _load_access_check()
    try:
        result = access_check.check_mapped_project_visibility(repo_root, probe=probe, env=env)
    except Exception as exc:  # noqa: BLE001 — advisory diagnostic must never crash fsck
        return {
            "status": "error",
            "reason": "probe_error",
            "required": [],
            "visible": [],
            "missing": [],
            "detail": repr(exc),
        }
    return {
        "status": result.status,
        "reason": "",
        "required": sorted(result.required),
        "visible": sorted(result.visible),
        "missing": list(result.missing),
        "detail": result.detail,
    }


def format_visibility_advisory(verdict: dict) -> list[str]:
    """Render the advisory live-visibility verdict as human-readable stderr lines."""
    status = verdict.get("status")
    detail = verdict.get("detail") or verdict.get("reason") or ""
    lines = ["--- Mapped-Project Live Visibility (advisory) ---"]
    if status == "ok":
        visible = ", ".join(verdict.get("visible", [])) or "(none required)"
        lines.append(f"  ok: every mapped key + legacy_default is visible to the bot [{visible}]")
    elif status == "missing":
        lines.append("  NOT visible to the bot: " + ", ".join(verdict.get("missing", [])))
        lines.append("  required: " + (", ".join(verdict.get("required", [])) or "(none)"))
        lines.append("  Fix projects.json / legacy_default (or the bot's project permissions).")
    elif status == "skipped":
        lines.append(f"  skipped: {detail}")
    elif status == "transport_unavailable":
        lines.append(f"  unavailable (could not verify): {detail}")
    else:
        lines.append(f"  {status}: {detail}")
    return lines
