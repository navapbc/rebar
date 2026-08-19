"""Structured Jira bridge access check shared by CLI, library, and MCP."""

from __future__ import annotations

import logging
import os
import time
import urllib.error
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from rebar_reconciler.adapters.jira.acli import AcliClient

JQL_RETRY_COUNT = 6
JQL_RETRY_SLEEP = 5

logger = logging.getLogger(__name__)


def _step(
    steps: list[dict[str, object]],
    lines: list[str],
    name: str,
    passed: bool,
    *,
    reason: str | None = None,
    detail: object | None = None,
) -> None:
    item: dict[str, object] = {"step": name, "passed": passed}
    if reason is not None:
        item["reason"] = reason
    if detail is not None:
        item["detail"] = str(detail)
    steps.append(item)
    if passed:
        lines.append(f"PROBE_PASS step={name}")
        return
    suffix = f" reason={reason}" if reason is not None else ""
    if detail is not None:
        suffix += f" detail={detail}"
    lines.append(f"PROBE_FAIL step={name}{suffix}")


def _retry_sleep(seconds: float) -> None:
    """The default JQL-retry sleeper: a patchable indirection over ``time.sleep``.

    Deliberately a named function rather than ``sleep_fn=time.sleep`` in the
    signature. A default expression is evaluated ONCE at import, capturing whatever
    ``time.sleep`` was then — so whether a test's ``time.sleep`` patch reached this
    retry depended on whether this module happened to be imported first, and the JQL
    loop really slept 5x5s whenever it did (ticket 5ea3-76e5-480a-4464). This
    indirection resolves ``time.sleep`` at CALL time, so the seam is reliably
    patchable while production behaviour is byte-identical. Mirrors
    ``acli_subprocess._backoff_sleep``, which exists for the same reason.
    """
    time.sleep(seconds)


def _resolve_probe_scope(
    env: dict[str, str] | None,
) -> tuple[dict[str, str] | None, tuple[dict[str, object], list[str], int] | None]:
    """Resolve the JIRA_* probe scope and fail closed when it is incomplete.

    Returns ``(creds, None)`` when credentials AND an explicit project are present, or
    ``(None, invalid_result)`` — an INVALID verdict + exit 2 — when either is missing.
    There is NO implicit ``DIG`` project default (AC2): an unset ``JIRA_PROJECT`` fails
    closed rather than silently probing a hard-coded project.
    """
    from rebar.config import resolve_jira_probe_scope

    src = os.environ if env is None else env
    jira_url, jira_user, jira_project = resolve_jira_probe_scope(env)
    creds = {
        "jira_url": jira_url,
        "jira_user": jira_user,
        "jira_api_token": src.get("JIRA_API_TOKEN", ""),
        "jira_project": jira_project,
    }
    if not creds["jira_url"] or not creds["jira_user"] or not creds["jira_api_token"]:
        return None, (
            {"verdict": "INVALID", "steps": [], "reason": "missing_credentials"},
            ["PROBE_FAIL reason=missing_credentials"],
            2,
        )
    if not creds["jira_project"]:
        return None, (
            {"verdict": "INVALID", "steps": [], "reason": "missing_project"},
            ["PROBE_FAIL reason=missing_project"],
            2,
        )
    return creds, None


def run_access_check(
    *,
    env: dict[str, str] | None = None,
    client_cls=AcliClient,
    sleep_fn=_retry_sleep,
) -> tuple[dict[str, object], list[str], int]:
    """Run the six-step probe and return its result, legacy lines, and exit code."""
    creds, invalid = _resolve_probe_scope(env)
    if invalid is not None:
        return invalid
    assert creds is not None
    jira_url = creds["jira_url"]
    jira_user = creds["jira_user"]
    jira_api_token = creds["jira_api_token"]
    jira_project = creds["jira_project"]

    probe_uuid = str(uuid.uuid4())
    label = f"rebar-id:{probe_uuid}"
    client = client_cls(
        jira_url=jira_url,
        user=jira_user,
        api_token=jira_api_token,
        jira_project=jira_project,
    )
    issue_key: str | None = None
    failed = False
    steps: list[dict[str, object]] = []
    lines: list[str] = []
    current_step = "STEP_CREATE"

    try:
        result = client.create_issue(
            {"title": f"rebar capability probe {probe_uuid}", "ticket_type": "task"}
        )
        issue_key = result.get("key") or result.get("id")
        if not issue_key:
            _step(
                steps, lines, current_step, False, reason="no_key_in_response", detail=repr(result)
            )
            failed = True
        else:
            _step(steps, lines, current_step, True)
            current_step = "STEP_LABEL"
            client._direct_rest_put_raw(
                f"/rest/api/3/issue/{issue_key}",
                {"update": {"labels": [{"add": label}]}},
            )
            _step(steps, lines, current_step, True)

            current_step = "STEP_PROPERTY_WRITE"
            client.set_issue_property(issue_key, "local_id", probe_uuid)
            _step(steps, lines, current_step, True)

            current_step = "STEP_JQL_SEARCH"
            jql = f'labels="{label}"'
            results: list[Any] = []
            for attempt in range(JQL_RETRY_COUNT):
                cache = getattr(client, "_search_cache", None)
                if isinstance(cache, dict):
                    cache.pop(jql, None)
                results = client.search_issues(jql)
                if results:
                    break
                if attempt < JQL_RETRY_COUNT - 1:
                    sleep_fn(JQL_RETRY_SLEEP)
            if results:
                _step(steps, lines, current_step, True)
            else:
                _step(steps, lines, current_step, False, reason="no_results_after_retry")
                failed = True

            current_step = "STEP_PROPERTY_READ"
            try:
                read_value = client.get_issue_property(issue_key, "local_id")
            except KeyError as exc:
                _step(steps, lines, current_step, False, reason="malformed_response", detail=exc)
                failed = True
            else:
                if read_value == probe_uuid:
                    _step(steps, lines, current_step, True)
                else:
                    detail = f"expected={probe_uuid} got={read_value}"
                    steps.append(
                        {
                            "step": current_step,
                            "passed": False,
                            "reason": "value_mismatch",
                            "detail": detail,
                        }
                    )
                    lines.append(f"PROBE_FAIL step={current_step} reason=value_mismatch {detail}")
                    failed = True
    except Exception as exc:  # noqa: BLE001 - the probe reports provider failures in-band
        steps.append(
            {"step": current_step, "passed": False, "reason": "exception", "detail": str(exc)}
        )
        lines.append(f"PROBE_FAIL reason=exception detail={exc}")
        failed = True
    finally:
        if issue_key is not None:
            try:
                client.delete_issue(issue_key)
                _step(steps, lines, "STEP_DELETE", True)
            except Exception as exc:  # noqa: BLE001 - cleanup is part of the probe verdict
                _step(steps, lines, "STEP_DELETE", False, reason="exception", detail=exc)
                failed = True

    verdict = "FAIL" if failed else "PASS"
    return {"verdict": verdict, "steps": steps}, lines, 1 if failed else 0


# ---------------------------------------------------------------------------
# Mapped-project visibility preflight (ticket a011).
#
# Before a reconcile pass mutates a live Jira, EVERY project the pass could
# touch must be visible to the bridge bot. This is the single source of truth
# for "is this mapped key + legacy_default (+ empty-mapping fallback) visible?"
# — reused by both the reconcile-pass preflight (__main__.run_pass_result) and
# the bridge fsck live diagnostic (ticket 9702). The check NEVER raises for a
# "not visible" / "unreachable" verdict (those are encoded in the returned
# ProjectVisibilityResult.status), so a diagnostic caller can inspect the full
# result; enforce_mapped_project_visibility() is the raising wrapper the
# fail-fast pass wiring uses.
# ---------------------------------------------------------------------------

_PROJECT_SEARCH_PATH = "/rest/api/3/project/search"
_PROJECT_SEARCH_PAGE_SIZE = 50
# Hard page cap: guards a backend that never sets ``isLast``. Hitting it fails
# CLOSED (see fetch_visible_project_keys) rather than trusting a partial list.
_PROJECT_SEARCH_PAGE_CAP = 200


class ProjectVisibilityError(RuntimeError):
    """One or more required project keys are not visible to the bridge bot.

    A REACHABLE backend that simply does not list a required key — distinct from
    :class:`TransportUnavailableError` (the backend could not be reached at all).
    """

    def __init__(
        self,
        missing: Iterable[str],
        *,
        required: Iterable[str] = (),
        visible: Iterable[str] = (),
    ) -> None:
        self.missing = list(missing)
        self.required = sorted(required)
        self.visible = sorted(visible)
        super().__init__(
            "reconcile preflight: mapped project(s) not visible to the bridge bot: "
            f"{', '.join(self.missing)}. Fix projects.json / legacy_default "
            "(or the bot's project permissions) before reconciling."
        )


class TransportUnavailableError(RuntimeError):
    """The visible-projects probe could not reach the backend (auth/transport).

    Fail-closed: the pass must abort rather than assume the mapped projects are
    fine. Deliberately NOT a subclass of :class:`ProjectVisibilityError` so a
    caller can distinguish "backend unreachable" from "project not listed".
    """


@dataclass(frozen=True)
class ProjectVisibilityResult:
    """Outcome of a mapped-project visibility check.

    ``status`` is one of:
      * ``"ok"``                    — every required key is visible (or nothing to check)
      * ``"missing"``               — a reachable backend does not list ``missing`` keys
      * ``"transport_unavailable"`` — the backend could not be reached (fail closed)
      * ``"skipped"``               — the transport has no visible-projects probe
    """

    status: str
    required: frozenset[str] = field(default_factory=frozenset)
    visible: frozenset[str] = field(default_factory=frozenset)
    missing: tuple[str, ...] = ()
    detail: str = ""


def _build_probe_client(env: dict[str, str] | None, client_cls) -> Any:
    """Build a Jira Cloud client from JIRA_* env, mirroring run_access_check.

    Missing credentials fail closed (they cannot be distinguished from an
    unreachable backend for preflight purposes).
    """
    from rebar.config import resolve_jira_probe_scope

    src = os.environ if env is None else env
    jira_url, jira_user, jira_project = resolve_jira_probe_scope(env)
    jira_api_token = src.get("JIRA_API_TOKEN", "")
    if not jira_url or not jira_user or not jira_api_token:
        raise TransportUnavailableError(
            "project-visibility probe: missing Jira credentials "
            "(JIRA_URL / JIRA_USER / JIRA_API_TOKEN)"
        )
    if not jira_project:
        raise TransportUnavailableError(
            "project-visibility probe: missing Jira project (JIRA_PROJECT); refusing "
            "to probe without an explicit target project"
        )
    return client_cls(
        jira_url=jira_url,
        user=jira_user,
        api_token=jira_api_token,
        jira_project=jira_project,
    )


def fetch_visible_project_keys(
    probe: Any,
    *,
    page_size: int = _PROJECT_SEARCH_PAGE_SIZE,
    page_cap: int = _PROJECT_SEARCH_PAGE_CAP,
) -> set[str]:
    """Return the set of project keys visible to the authenticated bot.

    ``probe`` is a Jira Cloud REST accessor exposing ``_direct_rest_get`` — the
    project-search endpoint below is Cloud-specific and is deliberately NOT part
    of the generic ``TicketTransport`` port, so this is a distinct probe seam.

    Walks the Jira Cloud "Get projects paginated" endpoint
    ``GET /rest/api/3/project/search`` — a PageBean (``values[]`` / ``startAt`` /
    ``maxResults`` / ``total`` / ``isLast``), NOT the token-cursor enhanced-search
    shape. The loop advances ``startAt`` by the page size and terminates on
    ``isLast``, an empty ``values`` page, or ``startAt >= total``.

    Relies on the probe's own transport retry (``_direct_rest_get`` ->
    ``_rest_urlopen_with_retry``: 3 attempts over transient faults, deterministic
    on HTTP errors) — it adds NO retry loop of its own. Any transport / HTTP /
    decode failure, and page-cap exhaustion without a clean termination, raise
    :class:`TransportUnavailableError` (fail-closed — a partial key set must never
    be treated as authoritative).
    """
    keys: set[str] = set()
    start_at = 0
    for _page in range(page_cap):
        path = f"{_PROJECT_SEARCH_PATH}?startAt={start_at}&maxResults={page_size}"
        try:
            data = probe._direct_rest_get(path)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as exc:
            raise TransportUnavailableError(
                f"project-visibility probe: cannot reach {path!r}: {exc!r}"
            ) from exc
        if not isinstance(data, dict):
            raise TransportUnavailableError(
                f"project-visibility probe: unexpected response from {path!r}: "
                f"{type(data).__name__}"
            )
        values = data.get("values")
        if not isinstance(values, list):
            values = []
        for proj in values:
            if isinstance(proj, dict) and proj.get("key"):
                keys.add(str(proj["key"]))
        if data.get("isLast", False) or not values:
            return keys
        start_at += len(values)
        total = data.get("total")
        if isinstance(total, int) and start_at >= total:
            return keys
    raise TransportUnavailableError(
        f"project-visibility probe: pagination did not terminate within {page_cap} pages; "
        "failing closed rather than trusting a partial project list"
    )


def required_project_keys(
    repo_root: str | os.PathLike[str],
    *,
    query_project: str | None = None,
    projects_store_mod: Any | None = None,
) -> set[str]:
    """Compute the set of project keys a reconcile pass could touch.

    Mirrors the fetcher fan-out (fetcher.py): ``read_projects().keys()``, or the
    single configured ``query_project`` fallback when the mapping is EMPTY (the
    green-state incident shape ``{legacy_default:'REB', projects:{}}``); PLUS
    ``legacy_default`` when set — every unbound/legacy create resolves there.
    """
    if projects_store_mod is None:
        from rebar_reconciler import projects_store as _ps_mod
    else:
        _ps_mod = projects_store_mod
    mapping = _ps_mod.load_mapping(repo_root)
    keys = {str(k) for k in mapping.projects.keys()}
    if not keys and query_project:
        keys = {str(query_project)}
    if mapping.legacy_default:
        keys.add(str(mapping.legacy_default))
    return keys


def check_mapped_project_visibility(
    repo_root: str | os.PathLike[str],
    *,
    probe: Any | None = None,
    client_cls=AcliClient,
    env: dict[str, str] | None = None,
    query_project: str | None = None,
    projects_store_mod: Any | None = None,
) -> ProjectVisibilityResult:
    """Single source of truth: is every mapped key + legacy_default visible?

    ``probe`` is an optional injected Jira Cloud REST accessor (exposing
    ``_direct_rest_get``); when ``None`` one is built from ``JIRA_*`` env via
    ``client_cls``. Returns a :class:`ProjectVisibilityResult`; never raises for a
    not-visible / unreachable / skipped verdict (those are in ``.status``). Reused
    by the reconcile-pass preflight and the bridge fsck diagnostic (ticket 9702).
    """
    required = required_project_keys(
        repo_root, query_project=query_project, projects_store_mod=projects_store_mod
    )
    if not required:
        return ProjectVisibilityResult(status="ok")

    if probe is None:
        try:
            probe = _build_probe_client(env, client_cls)
        except TransportUnavailableError as exc:
            return ProjectVisibilityResult(
                status="transport_unavailable",
                required=frozenset(required),
                detail=str(exc),
            )

    if not callable(getattr(probe, "_direct_rest_get", None)):
        detail = (
            "configured transport exposes no _direct_rest_get accessor; "
            "skipping visible-projects probe"
        )
        logger.warning("project-visibility preflight skipped: %s", detail)
        return ProjectVisibilityResult(
            status="skipped", required=frozenset(required), detail=detail
        )

    try:
        visible = fetch_visible_project_keys(probe)
    except TransportUnavailableError as exc:
        return ProjectVisibilityResult(
            status="transport_unavailable",
            required=frozenset(required),
            detail=str(exc),
        )

    missing = tuple(sorted(k for k in required if k not in visible))
    return ProjectVisibilityResult(
        status="ok" if not missing else "missing",
        required=frozenset(required),
        visible=frozenset(visible),
        missing=missing,
    )


def enforce_mapped_project_visibility(
    repo_root: str | os.PathLike[str], **kwargs: Any
) -> ProjectVisibilityResult:
    """Raise on a not-visible / unreachable verdict; return the result otherwise.

    The raising wrapper the fail-fast reconcile-pass preflight uses:
    :class:`ProjectVisibilityError` (naming the offending keys) on ``missing``,
    :class:`TransportUnavailableError` (fail-closed) on ``transport_unavailable``.
    An ``ok`` / ``skipped`` result is returned unchanged.
    """
    result = check_mapped_project_visibility(repo_root, **kwargs)
    if result.status == "missing":
        raise ProjectVisibilityError(
            result.missing, required=result.required, visible=result.visible
        )
    if result.status == "transport_unavailable":
        raise TransportUnavailableError(result.detail)
    return result
