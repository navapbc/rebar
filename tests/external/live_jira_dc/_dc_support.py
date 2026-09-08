"""Shared functions for the J11 live Data Center suites.

The non-``test_*`` name prevents pytest collection and external-test census. Functions shared
by the store-copy and mutation suites live here; fixtures remain conftest-exported because
imports do not register sibling fixtures and collection does not exercise setup resolution.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

BASE = os.environ.get("JIRA_DC_BASE_URL", "http://localhost:2990/jira")
# The harness's admin account — the ONE user guaranteed to exist and be assignable on a
# freshly provisioned instance, and the same value `conftest` uses to create the scratch
# project's lead. Read from the environment with the same default so the two cannot drift.
ADMIN_USER = os.environ.get("JIRA_DC_ADMIN", "admin")


def is_ticket_entry(name: str) -> bool:
    """Return whether a bare store entry is a ticket rather than a dot-prefixed marker.

    Structural filtering remains correct when the store gains markers such as ``.env-id``;
    enumerating known metadata caused the copy census to misclassify new markers as tickets.
    """
    return not name.startswith(".")


#: All variables that could target a real Jira, single-sourced for both fixture clearing and the
#: inherited-job assertion. The set includes the token and URL aliases as well as cloud defaults.
CLOUD_CREDENTIAL_VARS = (
    "JIRA_API_TOKEN",
    "JIRA_EMAIL",
    "ATLASSIAN_API_TOKEN",
    "JIRA_TOKEN",
    "JIRA_URL",
)

#: Where ``dc_store_copy_repo`` records the environment it INHERITED, before it changed anything.
#: The isolation cell reads this rather than ``os.environ``: after the fixture runs, ``os.environ``
#: reflects the fixture's own edits, so asserting on it proves only that the fixture ran.
INHERITED_ENV_FILE = ".j11-inherited-env.json"


def read_inherited_env(work: Path) -> dict[str, str | None]:
    """Read the pre-fixture job environment, failing if its snapshot is absent.

    Returning an empty mapping for a missing file would make the isolation assertions vacuous.
    """
    path = work / INHERITED_ENV_FILE
    assert path.is_file(), (
        f"{INHERITED_ENV_FILE} is absent from {work} — the fixture did not record the inherited "
        f"environment, so any assertion about the JOB environment would pass vacuously"
    )
    return dict(json.loads(path.read_text()))


def collect_base_urls(root: Path) -> dict[str, list[str]]:
    """Map every configured ``base_url`` to its declaring files beneath ``root``.

    Search ``rebar.toml``, ``pyproject.toml``, and ``.rebar/``—the surfaces a pass reads. Keeping
    the scanner separate lets a unit test prove it detects a foreign URL rather than comparing
    only the fixture-generated configuration with itself.
    """
    import re

    pattern = re.compile(r"""^\s*base_url\s*=\s*["']([^"']+)["']""", re.MULTILINE)
    candidates = [root / "rebar.toml", root / "pyproject.toml"]
    rebar_dir = root / ".rebar"
    if rebar_dir.is_dir():
        candidates.extend(sorted(p for p in rebar_dir.rglob("*") if p.is_file()))
    collected: dict[str, list[str]] = {}
    for path in candidates:
        if not path.is_file():
            continue
        for value in pattern.findall(path.read_text()):
            collected.setdefault(value, []).append(str(path.relative_to(root)))
    return collected


def derive_rename_target(project_key: str) -> str:
    """Derive a valid harness project key guaranteed to differ from ``project_key``.

    Replacing the last character with ``Z`` unless it is already ``Z`` (then ``Y``) eliminates
    the former one-in-26 collision while preserving length, uppercase characters, and prefix.
    """
    replacement = "Y" if project_key.endswith("Z") else "Z"
    stem = project_key[:-1] if len(project_key) >= 4 else project_key
    return f"{stem}{replacement}"


def live_jira_ready() -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{BASE}/rest/api/2/serverInfo", timeout=5) as resp:
            return bool(resp.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def jira_extra_installed() -> bool:
    try:
        import jira  # noqa: F401
    except ImportError:
        return False
    return True


skip_no_harness = pytest.mark.skipif(
    not live_jira_ready(),
    reason=(
        f"Jira DC harness not reachable at {BASE}; start it with "
        "`docker compose -f tests/external/live_jira_dc/docker-compose.yml up -d`"
    ),
)
#: A reachable harness without the Jira extra must fail, not silently skip every DC cell.
extra_missing_but_harness_up = live_jira_ready() and not jira_extra_installed()

skip_no_extra = pytest.mark.skipif(
    not jira_extra_installed() and not extra_missing_but_harness_up,
    reason="the [jira-datacenter] extra is not installed",
)


def fail_if_extra_missing_while_harness_is_up() -> None:
    """Turn a silent all-skip into a loud failure when the harness is reachable."""
    if extra_missing_but_harness_up:
        pytest.fail(
            f"the Jira DC harness is reachable at {BASE} but the 'jira-datacenter' extra "
            "(pycontribs/jira) is NOT installed, so these tests would silently skip and this "
            "run would report green having validated nothing. Install it with: "
            "pip install -e '.[dev,jira-datacenter]'"
        )


def source_repo_root() -> Path:
    """The checkout this test file lives in — the SOURCE of the store copy."""
    return Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=Path(__file__).resolve().parent,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
    )


def run_reconcile(repo: Path, mode: str, *, only: str | None = None):
    """Invoke the reconciler subprocess directly so BOTH streams are observable.

    ``only`` maps to ``--filter-local-ids``. Scoping is MANDATORY for writing passes here:
    the scrub removes every binding, so an unscoped writing pass would route the whole copied
    store down the CREATE path (`outbound_differ.py:518-520`) and file production tickets as
    new harness issues.
    """
    from rebar._engine import engine_env

    argv = [sys.executable, "-m", "rebar_reconciler", "--mode", mode, "--repo-root", str(repo)]
    if only is not None:
        argv += ["--filter-local-ids", only]
    return subprocess.run(
        argv, env=engine_env(str(repo)), text=True, capture_output=True, check=False
    )


def run_bridge(
    repo: Path,
    command: str,
    *,
    only: str | None = None,
    max_changes: int | None = None,
):
    """Invoke a primary ``preview`` or ``sync`` reconciler command.

    Unlike the retained ``run_reconcile`` compatibility helper, ``only`` uses the
    primary selection contract and therefore narrows examination as well as writes.
    """
    from rebar._engine import engine_env

    argv = [sys.executable, "-m", "rebar_reconciler", command]
    if max_changes is not None:
        argv += ["--max-changes", str(max_changes)]
    if only is not None:
        argv += ["--only", only]
    argv += ["--repo-root", str(repo)]
    return subprocess.run(
        argv, env=engine_env(str(repo)), text=True, capture_output=True, check=False
    )


def envelope(cp) -> dict[str, Any]:
    out = cp.stdout.strip()
    for line in reversed([ln for ln in out.splitlines() if ln.strip()]):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise AssertionError(f"no JSON envelope on stdout:\n{out}\n--stderr--\n{cp.stderr}")


def wait_until_searchable(transport: Any, project: str, key: str, timeout: float = 90.0) -> None:
    """Wait until JQL can see ``key``, or fail with an explicit index-lag diagnosis.

    Direct creation precedes Jira's eventually consistent Lucene index. Waiting prevents an
    inbound zero-total result from being misreported as a bridge defect when search simply has
    not observed the existing issue yet.
    """
    import time

    deadline = time.monotonic() + timeout
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        hits = transport.search_issues(f'project = "{project}" AND key = "{key}"')
        if any(h.get("key") == key for h in hits):
            return
        time.sleep(2.0)
    raise AssertionError(
        f"{key} was created but never became searchable within {timeout:.0f}s "
        f"({attempts} attempts) — Jira's index is lagging further than this suite allows. "
        "This is NOT a bridge defect: the issue exists, the search cannot see it."
    )


def force_issue_reindex(dc_request: Any, key: str) -> tuple[int, Any]:
    """Request synchronous per-issue indexing so JQL can observe ``key`` deterministically.

    Background Lucene latency is unbounded under CI load, while the admin resource indexes the
    numeric issue ID immediately. This is only an accelerator: an unreadable or ID-less issue,
    or an unavailable reindex endpoint, returns its response without raising so callers retain
    their existing search-wait fallback.
    """
    status, body = dc_request(f"/rest/api/2/issue/{key}?fields=id")
    if status != 200 or not isinstance(body, dict):
        return status, body
    issue_id = body.get("id")
    if not issue_id:
        return status, body
    return dc_request(
        f"/rest/api/2/reindex/issue?issueId={issue_id}"
        "&indexComments=true&indexChangeHistory=true&indexWorklogs=false",
        method="POST",
    )


def seed_searchable_issue(
    transport: Any,
    project: str,
    track_issue: Any,
    summary: str,
    *,
    issuetype: str = "Task",
    extra: dict[str, Any] | None = None,
) -> str:
    """Create an issue in DC and return its key once a JQL search can see it."""
    transport.project = project
    payload: dict[str, Any] = {"summary": summary, "issuetype": issuetype}
    if extra:
        payload.update(extra)
    created = transport.create_issue(payload)
    key = created["key"]
    track_issue(key)
    wait_until_searchable(transport, project, key)
    return key


def read_local_ticket(repo: Path, local_id: str) -> dict[str, Any]:
    """The local ticket as JSON — the inbound oracle's read side.

    Reads the store through the library rather than by parsing event files, so the oracle
    sees the same PROJECTION the product serves rather than a re-implementation of it.
    """
    import rebar

    return rebar.show_ticket(local_id, repo_root=repo)


def forget_identity_mapping(repo: Path, provider: str, external_id: str) -> list[str]:
    """Remove all copied-store identities for ``(provider, external_id)`` and return their IDs.

    The scrub starts unmapped, but ``bound_dc_issue`` can mint the default-assigned admin during
    its binding pass, so the mint oracle must restore absence afterward. Removing identity
    directories is safe in this remote-free throwaway copy and makes the working-tree reader
    miss them. Drain duplicate carriers, but fail if a removed ID resolves again to avoid a spin.
    """
    import shutil

    import rebar

    removed: list[str] = []
    tracker = Path(repo) / ".tickets-tracker"
    while True:
        identity_id = rebar.resolve_mapping(provider, external_id, repo_root=repo)
        if identity_id is None:
            return removed
        if identity_id in removed:
            raise AssertionError(
                f"{provider}/{external_id!r} still resolves to {identity_id!r} after that "
                f"identity was removed from {tracker} — removal is not what makes "
                f"resolve_mapping miss, so the oracle's precondition cannot be established"
            )
        directory = tracker / identity_id
        if not directory.is_dir():
            raise AssertionError(
                f"{provider}/{external_id!r} resolves to {identity_id!r} but there is no "
                f"ticket directory at {directory} to remove"
            )
        shutil.rmtree(directory)
        removed.append(identity_id)


def assert_mint_registered(repo: Path, external_id: str) -> str:
    """Assert that inbound minted one shared-provider placeholder and return its identity ID.

    The helper exposes the exact live oracle to harness-free mutation tests. Its pure mapping
    reads cannot create identities, so prior absence attributes the result to the pass; a
    ``jira-datacenter`` mapping would incorrectly fork the shared ``jira`` namespace.
    """
    import rebar

    minted = rebar.resolve_mapping("jira", external_id, repo_root=repo)
    assert minted is not None, (
        f"THE PASS MINTED NOTHING: jira/{external_id!r} still resolves to no identity in the "
        f"store copy after an inbound pass that carried that assignee. This is bug 5f48's "
        f"silent-swallow signature — the mint is best-effort and swallows its own failure, so "
        f"the registry is the only place it is observable."
    )
    assert rebar.is_placeholder(minted, repo_root=repo), (
        f"the identity the pass minted for {external_id!r} ({minted!r}) is not a PLACEHOLDER. "
        f"The inbound mint is documented to create a GHOST identity a later outbound pass can "
        f"key on; a non-placeholder means it adopted or overwrote a real person's identity."
    )
    forked = rebar.resolve_mapping("jira-datacenter", external_id, repo_root=repo)
    assert forked is None, (
        f"the DC pass ALSO minted under a `jira-datacenter` provider ({forked!r}), forking the "
        f"identity namespace the epic decided the two deployments share. The deployment belongs "
        f"in `RemoteRef.instance`, not in the provider string."
    )
    return minted


# Exact J11 oracles live here so harness-free tests can execute—not paraphrase—the Linux-only
# live checks and prove that each discriminates red from green.


def assert_local_assignee_is(
    ticket: dict[str, Any], expected_user: str, *, stage: str = "the inbound assign"
) -> None:
    """Assert that local ``assignee`` exactly equals this DC username (or is empty).

    The seeded issue may already carry the project lead, so truthiness can pass before the
    mutation; the cell first proves an empty precondition, then checks the value only the pass
    could write. Local tickets store DC's human-readable ``name`` rather than a registry ID;
    placeholder registration is asserted separately by ``assert_mint_registered``.
    """
    got = ticket.get("assignee") or ""
    if not expected_user:
        assert not got, (
            f"{stage}: the local ticket is STILL ASSIGNED to {got!r}, expected the assignee to "
            f"be EMPTY. Until it is empty, an assignment afterwards is not a CHANGE and the "
            f"oracle below could pass on the value the binding pass already imported."
        )
        return
    assert got == expected_user, (
        f"{stage}: the local .assignee is {ticket.get('assignee')!r}, expected EXACTLY "
        f"{expected_user!r} — the DC username `_extract_name` puts on the ticket "
        f"(`apply_inbound_records.py:210,370` -> `inbound_translate.py:285-294`, which prefers "
        f"`name` over `displayName`). A non-empty but DIFFERENT value means the pass did not "
        f"carry this assignment: the value on the ticket is the one the binding pass imported "
        f"when the seeded issue arrived default-assigned to the project lead."
    )


#: Writers emit ``rebar-id:<local_id>``. The hyphen form is read-only compatibility, so an
#: outbound issue carrying only that legacy label is a failure, not an equivalent marker.
REBAR_ID_LABEL_PREFIX = "rebar-id:"
LEGACY_REBAR_ID_LABEL_PREFIX = "rebar-id-"


def assert_outbound_provenance_markers(
    local_id: str, labels: list[Any], property_status: int, property_body: Any
) -> None:
    """Assert that a created issue carries both outbound provenance markers.

    Dedup JQL consumes the colon-form label; inbound correlation consumes the entity property,
    so neither substitutes for the other. The property comes from raw REST rather than the
    writing abstraction, allowing the oracle to detect a consistently re-read but incorrectly
    wrapped value.
    """
    expected_label = f"{REBAR_ID_LABEL_PREFIX}{local_id}"
    label_strings = [lbl for lbl in labels if isinstance(lbl, str)]
    if expected_label not in label_strings:
        legacy = f"{LEGACY_REBAR_ID_LABEL_PREFIX}{local_id}"
        hint = (
            f" The issue carries the LEGACY HYPHEN form {legacy!r} instead. That form is "
            f"read-only compatibility (`binding_walk.py:352`, `inbound_translate.py:77-78`); "
            f"no writer emits it, so an issue created by this pass carrying it means the "
            f"create wrote through an unexpected path."
            if legacy in label_strings
            else ""
        )
        raise AssertionError(
            f"the created DC issue does NOT carry the provenance label {expected_label!r} — "
            f"labels are {label_strings!r}. The outbound create writes it at "
            f"`dispatch_one.py:306`; without it the dedup JQL at `dispatch_one.py:214` cannot "
            f"re-find the issue and the next pass creates a DUPLICATE.{hint}"
        )
    assert property_status == 200, (
        f"the entity property `local_id` is NOT READABLE on the created DC issue: raw REST "
        f"GET .../properties/local_id returned HTTP {property_status} (body "
        f"{str(property_body)[:200]}). The outbound create writes it at "
        f"`dispatch_one.py:307`; a 404 means the write never landed, and the label alone does "
        f"not satisfy row 1 — inbound consumers correlate on the property."
    )
    assert isinstance(property_body, dict), (
        f"the entity-property read returned {property_body!r}, not a JSON object; the endpoint "
        f"returns {{'key': 'local_id', 'value': …}} and the oracle cannot read a value out of "
        f"anything else."
    )
    value = property_body.get("value")
    assert value == local_id, (
        f"the entity property `local_id` on the created DC issue is {value!r}, expected the "
        f"local id {local_id!r} VERBATIM. The value is PUT unwrapped "
        f"(`jira_datacenter/transport.py:615-632` — 'the value is passed verbatim'), so a "
        f"nested {{'value': …}} here is bug 0b27's wrong-shape signature: stored without "
        f"raising, and correlation silently broken."
    )


def raw_indexed_issue_count(
    dc_request: Any, project: str, *, page_size: int = 50, max_requests: int = 200
) -> int:
    """Count indexed project issues through raw REST paging, independent of ``_paged_search``.

    This keeps a pagination regression out of its own precondition oracle. Advance by the
    number actually returned because DC may clamp ``maxResults``; a short page is not EOF, and
    advancing by the request size would skip the withheld rows.
    """
    seen: set[str] = set()
    start_at = 0
    for _ in range(max_requests):
        status, body = dc_request(
            f"/rest/api/2/search?jql=project%3D{project}"
            f"&startAt={start_at}&maxResults={page_size}&fields=key"
        )
        assert status == 200 and isinstance(body, dict), (
            f"the raw paged count for {project} failed at startAt={start_at}: HTTP {status}, "
            f"body {str(body)[:200]}"
        )
        issues = [i for i in (body.get("issues") or []) if isinstance(i, dict)]
        if not issues:
            return len(seen)
        seen.update(str(i["key"]) for i in issues if i.get("key"))
        start_at += len(issues)
        total = body.get("total")
        if isinstance(total, int) and start_at >= total:
            return len(seen)
    raise AssertionError(
        f"the raw paged count for {project} did not terminate within {max_requests} requests "
        f"(startAt={start_at}, {len(seen)} distinct keys). Either the project holds more issues "
        f"than this measurement is budgeted for, or the search endpoint is returning pages "
        f"without advancing — do NOT read the partial count as an index-lag verdict."
    )


def assert_remote_parent_is(
    key: str,
    issue_status: int,
    issue_body: Any,
    expected_parent: str,
    *,
    previous_parent: str = "",
    stage: str = "the outbound parent set",
) -> None:
    """Assert from raw REST that ``key`` has exactly ``expected_parent``.

    Avoid the writing transport, whose client object may echo its mutation, and the eventually
    consistent paged search used by inbound reads. ``previous_parent`` distinguishes DC's
    silent-no-op signature (unchanged) from clearing or writing the wrong parent.
    """
    assert issue_status == 200 and isinstance(issue_body, dict), (
        f"{stage}: {key} is not readable by raw REST (HTTP {issue_status}, body "
        f"{str(issue_body)[:200]}), so the parent cannot be asserted at all."
    )
    parent = (issue_body.get("fields") or {}).get("parent")
    got = parent.get("key") if isinstance(parent, dict) else None
    if got == expected_parent:
        return
    if previous_parent and got == previous_parent:
        raise AssertionError(
            f"{stage}: fields.parent on {key} is STILL {got!r} — the parent it had BEFORE the "
            f"mutation. Expected {expected_parent!r}. This is the silent-no-op signature: "
            f"`set_parent` writes `fields.parent` for a sub-task "
            f"(`jira_datacenter/transport.py:711-712`) and every core caller swallows its "
            f"failure (`dispatch_one.py:571-578` warns and continues), so an unchanged field is "
            f"the ONLY place the failure is observable."
        )
    raise AssertionError(
        f"{stage}: fields.parent on {key} is {parent!r} (key {got!r}), expected {expected_parent!r}"
        + (f" (it was {previous_parent!r} before)" if previous_parent else "")
        + ". A null/absent parent means the write cleared the field instead of setting it; any "
        "other key means it landed on the wrong issue."
    )


_ALERT_STORE_SUBPATH = ("bridge_state", "bridge_alerts")


def assert_bridge_alert_for_mutation(
    cp: subprocess.CompletedProcess,
    repo: Path,
    local_id: str,
    *,
    key: str | None = None,
) -> list[dict[str, Any]]:
    """Return mutation alerts for ``local_id`` or ``key`` after proving the pass completed.

    Absence is meaningful only after a zero-exit, traceback-free pass: the alert directory is
    created on first write, so a missing directory otherwise also means nobody ran. The caller
    interprets the possibly empty matches; either identifier can be absent from a failed create
    or update record.
    """
    assert cp.returncode == 0, (
        f"the pass exited {cp.returncode}, not 0 — a failed pass's alert store proves nothing "
        f"about whether a MUTATION was swallowed, only that the pass itself did not complete. "
        f"stdout:\n{cp.stdout[-1500:]}\nstderr:\n{cp.stderr[-1500:]}"
    )
    assert "Traceback" not in cp.stderr, (
        f"the pass raised (a traceback is on stderr), so it did not run to completion and its "
        f"alert store is not evidence of anything either way:\n{cp.stderr[-2000:]}"
    )

    store_dir = repo.joinpath(*_ALERT_STORE_SUBPATH)
    records: list[dict[str, Any]] = []
    if store_dir.is_dir():
        for jsonl_file in sorted(store_dir.glob("*.jsonl")):
            for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("local_id") == local_id or (key is not None and rec.get("key") == key):
                    records.append(rec)
    return records


def probe_subtask_parent_put(
    dc_request: Any, key: str, new_parent: str | None, *, verb: str = "fields"
) -> tuple[int, Any]:
    """Capture DC's raw response to setting or clearing a subtask parent.

    The pycontribs path hides HTTP status and body. ``fields`` sends the production shape;
    ``update`` sends the alternative set operation, so disagreement isolates payload shape from
    reparenting support. ``new_parent=None`` clears. This diagnostic returns evidence without
    judging it.
    """
    target = {"key": new_parent} if new_parent else None
    if verb == "update":
        payload: dict[str, Any] = {"update": {"parent": [{"set": target}]}}
    else:
        payload = {"fields": {"parent": target}}
    return dc_request(f"/rest/api/2/issue/{key}", method="PUT", payload=payload)


def probe_subtask_parent_editmeta_ops(dc_request: Any, key: str) -> tuple[int, list[str]]:
    """Return the parent operations advertised by ``editmeta`` as a diagnostic falsifier.

    Missing operations implicate field editability rather than an accepted write's silent
    no-op. A non-200 or non-object response returns ``(status, [])``, preserving the distinction
    between no advertised operations and a failed metadata read.
    """
    status, body = dc_request(f"/rest/api/2/issue/{key}/editmeta")
    if status != 200 or not isinstance(body, dict):
        return status, []
    parent_meta = (body.get("fields") or {}).get("parent") or {}
    ops = parent_meta.get("operations") or []
    return status, [str(op) for op in ops]
