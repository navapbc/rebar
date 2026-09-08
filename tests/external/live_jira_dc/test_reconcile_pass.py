"""Run the complete reconciler against the live J5 Data Center harness (J7, epic e369).

The DC config, registry, Jira-family backend, and transport must compose without unhandled
subprocess errors; an immediate repeat must write nothing. Inbound tickets retain shared
``jira`` provenance while ``RemoteRef.instance`` distinguishes deployment. The live sentinel
enrolls the all-skip canary: absent harness skips, but a reachable harness without the DC
extra fails loudly. Plain HTTP deliberately exercises ``allow_insecure``.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from _bridge_output import converged_pass_problem, wrote_nothing_problem
from _dc_support import run_bridge as _run_bridge

_BASE = os.environ.get("JIRA_DC_BASE_URL", "http://localhost:2990/jira")


def _live_jira_ready() -> bool:
    """The sentinel ``tests/external/conftest.py`` keys on to apply ``jira_live``."""
    try:
        req = urllib.request.Request(f"{_BASE.rstrip('/')}/rest/api/2/serverInfo")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _jira_extra_installed() -> bool:
    try:
        import jira  # noqa: F401
    except ImportError:
        return False
    return True


_skip = pytest.mark.skipif(
    not _live_jira_ready(),
    reason=(
        "Jira DC harness not reachable at "
        f"{_BASE} — start it with `make jira-dc-up` and run with REBAR_RUN_EXTERNAL=1"
    ),
)

# See the module docstring: a missing extra is a legitimate skip ONLY when there is no
# harness either. Harness up + extra absent is a broken environment, and silently skipping
# would let the run certify code that never executed.
_extra_missing_but_harness_up = _live_jira_ready() and not _jira_extra_installed()

_skip_no_extra = pytest.mark.skipif(
    not _jira_extra_installed() and not _extra_missing_but_harness_up,
    reason="the 'jira-datacenter' extra (pycontribs/jira) is not installed — "
    "pip install 'nava-rebar[jira-datacenter]'",
)


@pytest.fixture(autouse=True)
def _fail_if_extra_missing_while_harness_is_up() -> None:
    """Turn "harness reachable but extra absent" into a LOUD failure."""
    if _extra_missing_but_harness_up:
        pytest.fail(
            "the Jira DC harness is reachable at "
            f"{_BASE} but the 'jira-datacenter' extra (pycontribs/jira) is NOT "
            "installed, so this live reconcile module would silently skip and this run "
            "would report green having validated nothing. Install it with: "
            "pip install -e '.[dev,jira-datacenter]'"
        )


# ---------------------------------------------------------------------------
# The DC-configured local repo — the half the J5 harness does NOT provision
# ---------------------------------------------------------------------------


@pytest.fixture
def dc_rebar_repo(
    rebar_repo: Path, jira_dc_project: str, jira_dc_pat: str, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Configure the temporary repo for the DC backend and its exact scratch project.

    The inherited repo has no reconciler settings. This adds the backend and harness URL;
    ``allow_insecure`` is required because config validation rejects its plain HTTP URL.
    """
    (rebar_repo / "rebar.toml").write_text(
        textwrap.dedent(f"""
        [reconciler]
        backend = "jira-datacenter"
        base_url = "{_BASE}"
        allow_insecure = true

        [jira]
        project = "{jira_dc_project}"
        """).lstrip()
    )
    # The PAT is env-only by design (never a config key, so it cannot be committed).
    # `engine_env` builds the subprocess environment as `dict(os.environ)`, so setting it
    # here reaches the reconciler child process.
    monkeypatch.setenv("JIRA_PAT", jira_dc_pat)
    monkeypatch.setenv("JIRA_PROJECT", jira_dc_project)
    return rebar_repo


def _envelope(cp: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Parse the reconciler's JSON result envelope from stdout (last JSON line)."""
    out = cp.stdout.strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        for line in reversed([ln for ln in out.splitlines() if ln.strip()]):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise AssertionError(f"no JSON envelope on reconciler stdout:\n{out}\n--stderr--\n{cp.stderr}")


def _assert_converged_writing_pass(cp: subprocess.CompletedProcess[str], *, what: str) -> None:
    """Require the canonical sync completion signal, ``BRIDGE_STATE: converged``.

    Writing sync emits neither a JSON envelope nor the legacy ``OK:`` summary; the shared
    stderr parser has offline coverage.
    """
    problem = converged_pass_problem(cp.stdout, cp.stderr)
    assert problem is None, f"{what}: {problem}\n{cp.stdout}\n--stderr--\n{cp.stderr}"


def _assert_wrote_nothing(cp: subprocess.CompletedProcess[str], *, what: str) -> None:
    """Prove idempotence from zero differ totals and no mutation batch outcome.

    ``BRIDGE_STATE: converged`` only means settled and may still include writes; the
    unconditional ``RECON:`` counters carry the zero-write claim.
    """
    problem = wrote_nothing_problem(cp.stdout, cp.stderr)
    assert problem is None, (
        f"{what}: the repeated pass was not a no-op — {problem}:\n"
        f"{cp.stdout}\n--stderr--\n{cp.stderr}"
    )


def _assert_no_unhandled_exception(cp: subprocess.CompletedProcess[str], *, what: str) -> None:
    """Epic AC6: a swallowed exception can still 'converge', so convergence alone is not
    evidence. Assert the run surfaced no traceback and exited cleanly."""
    assert "Traceback" not in cp.stderr, (
        f"{what}: the reconcile pass raised an unhandled exception:\n{cp.stderr}"
    )
    assert cp.returncode in (0, 75), (
        f"{what}: reconcile exited {cp.returncode}\n--stdout--\n{cp.stdout}\n"
        f"--stderr--\n{cp.stderr}"
    )


# ---------------------------------------------------------------------------
# 1. a pass completes with no unhandled exception
# ---------------------------------------------------------------------------


@_skip
@_skip_no_extra
def test_dc_reconcile_pass_raises_no_unhandled_exception(dc_rebar_repo: Path) -> None:
    cp = _run_bridge(dc_rebar_repo, "preview")
    _assert_no_unhandled_exception(cp, what="preview")

    # Preview IS a no_write operation, so it DOES emit a JSON envelope: ``__main__.py``
    # calls json.dumps only on the no_write branch, and that call is NOT route-gated (only
    # the ``OK:`` summary beside it is). So this cell survived the move to the canonical
    # routes unchanged — keep the stronger assertion here; only the two WRITING-pass
    # helpers above had to change.
    envelope = _envelope(cp)
    assert envelope.get("mutation_failures", 0) == 0, (
        f"the preview reported mutation failures: {envelope}"
    )


# ---------------------------------------------------------------------------
# 2. idempotence — the postcondition that proves CONVERGENCE, not mere writing
# ---------------------------------------------------------------------------


@_skip
@_skip_no_extra
def test_a_repeated_dc_reconcile_pass_writes_nothing(dc_rebar_repo: Path) -> None:
    """A second pass immediately after the first must be a no-op. Writing on every pass
    would still look like 'success' on a single run while thrashing the remote forever."""
    first = _run_bridge(dc_rebar_repo, "sync", max_changes=10)
    _assert_no_unhandled_exception(first, what="first pass")

    second = _run_bridge(dc_rebar_repo, "sync", max_changes=10)
    _assert_no_unhandled_exception(second, what="second pass")

    _assert_wrote_nothing(second, what="second pass")


# ---------------------------------------------------------------------------
# 3. provenance — the epic's shared-identity decision
# ---------------------------------------------------------------------------


@_skip
@_skip_no_extra
def test_a_dc_created_ticket_carries_the_shared_jira_provenance(
    dc_rebar_repo: Path, jira_dc_project: str, jira_dc_pat: str, track_issue: Any
) -> None:
    """Keep shared ``jira`` provenance while ``RemoteRef.instance`` identifies DC.

    Inbound tickets retain the ``jira-`` ID prefix, so deployment adds no store vocabulary
    or migration.
    """
    from rebar_reconciler.adapters.jira_datacenter.settings import JiraDataCenterSettings
    from rebar_reconciler.adapters.jira_datacenter.transport import (
        JiraDataCenterTransport,
        build_client_from_settings,
    )

    import rebar

    settings = JiraDataCenterSettings(
        url=_BASE,
        project=jira_dc_project,
        allow_insecure=True,
        ca_bundle="",
        pat=jira_dc_pat,
    )
    transport = JiraDataCenterTransport(
        client=build_client_from_settings(settings), project=jira_dc_project
    )
    created = transport.create_issue(
        {
            "project": jira_dc_project,
            "summary": "J7 provenance oracle",
            "description": "created directly on the DC harness",
            "issuetype": "Task",
        }
    )
    remote_key = created["key"]
    track_issue(remote_key)

    cp = _run_bridge(dc_rebar_repo, "sync", max_changes=10)
    _assert_no_unhandled_exception(cp, what="inbound provenance pass")
    _assert_converged_writing_pass(cp, what="inbound provenance pass")

    # Match the exact derived ID: derivation lowercases the key and the create payload may omit
    # its raw form, while a serialized substring could match an unrelated field (bug 23ed).
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    expected_local_id = _jira_key_to_local_id(remote_key)
    tickets = rebar.list_tickets(repo_root=str(dc_rebar_repo))
    matched = [t for t in tickets if t.get("ticket_id") == expected_local_id]
    assert matched, (
        f"no local ticket was created from DC issue {remote_key} — expected local id "
        f"{expected_local_id!r}, saw {sorted(t.get('ticket_id') for t in tickets)!r}; "
        f"stdout={cp.stdout!r}"
    )
    ticket = matched[0]

    # The BINDING is where the DC<->local correspondence actually lives, and it is what the
    # epic's headline criterion is really about. Asserting the ticket exists proves a ticket
    # was created; asserting the binding proves it is bound to THIS DC issue.
    from rebar_reconciler.binding_store import load_binding_store

    bindings = load_binding_store(dc_rebar_repo)
    assert bindings.get_jira_key(expected_local_id) == remote_key, (
        f"local ticket {expected_local_id!r} is not bound to DC issue {remote_key!r} "
        f"(binding: {bindings.get_jira_key(expected_local_id)!r}) — the ticket exists but "
        f"the store does not record which DC issue it came from"
    )
    assert ticket.get("creation_channel") == "jira", (
        "a DC-created ticket must carry the SHARED 'jira' creation channel — the "
        f"deployment is distinguished by RemoteRef.instance, not a new channel: {ticket}"
    )
