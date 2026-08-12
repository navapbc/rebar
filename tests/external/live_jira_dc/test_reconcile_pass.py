"""Live END-TO-END reconcile pass against the J5 Data Center harness (story J7, epic e369).

This is the point at which the whole DC assembly — config selection, the registry, the
backend, the shared Jira-family layer and the DC transport — first drives a REAL Jira
instance as one system. Everything below it is proven in isolation; only a live pass
proves they compose.

What it pins:

1. a reconcile pass against a DC-configured repo completes with ZERO unhandled exceptions;
2. it is IDEMPOTENT — an immediately repeated pass writes nothing (convergence, not merely
   "it wrote something");
3. provenance: a ticket created locally FROM a DC issue carries the shared ``jira``
   creation channel and a ``jira-`` local-id prefix, with the deployment distinguished by
   ``RemoteRef.instance`` — the epic's shared-identity decision.

WHY THE EXCEPTION CHECK IS A SUBPROCESS ASSERTION, not ``caplog``: the bridge runs the
reconciler in a SUBPROCESS. ``caplog`` hooks only the *test* process's ``logging`` and can
never observe those records. These tests therefore invoke the primary reconciler subprocess
vocabulary DIRECTLY and assert on both captured streams.

Tier notes (inherited from ``tests/external/``; see ``test_transport.py`` for the full
rationale, reproduced here only where it matters):

* the module-level ``_live_jira_ready`` sentinel is what makes ``tests/external/conftest.py``
  attach the ``jira_live`` marker and enrol this module in the all-skip canary;
* absent harness ⇒ SKIP with an actionable message. But when the harness IS reachable and
  the ``[jira-datacenter]`` extra is absent, that is a LOUD FAILURE, not a skip: the canary
  counts collected-vs-executed GLOBALLY per session, so a sibling module's executing tests
  mask an all-skip of this one and the job reports green having validated nothing. J6
  shipped two defects behind exactly that mask;
* the harness serves plain ``http://localhost:2990/jira``, so the config written here sets
  ``allow_insecure = true`` explicitly — J6's TLS validator REJECTS a non-``https``
  ``base_url`` at config-load time otherwise, so this exercises the override branch rather
  than bypassing the validator.
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
    """A rebar repo wired to drive the DC backend.

    ``rebar_repo`` (inherited from the PARENT conftest, ``tests/external/conftest.py``)
    gives us an initialized store in a temp git dir — but it calls ``rebar.init_repo()``
    and writes NO ``[tool.rebar.reconciler]`` section, so on its own the reconcile
    subprocess would have no backend, no base URL and no credential: it would fail at
    config-load or silently drive the CLOUD backend. This fixture supplies exactly that
    missing half.

    ``allow_insecure`` is REQUIRED, not incidental — the harness is plain http and the
    config-load TLS validator rejects a non-https ``base_url`` without it.
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
    """Assert a WRITING pass settled, reading the shape the CANONICAL route actually emits.

    Writing passes emit no JSON envelope — only the ``no_write`` branch calls ``json.dumps``.
    They used to be read off the ``OK: …`` stdout summary, but the canonical ``preview`` /
    ``sync`` routes suppress that line by design (``__main__.py`` guards it with
    ``route not in {"preview", "sync"}``) and report ``BRIDGE_STATE: converged`` on stderr
    instead — see ``docs/exit-codes.md`` §"Bridge routes" and ADR 0092. These cells were
    re-pointed at ``sync`` by ``c87afedba8``, so the ``OK:`` form asserted on output the
    invoked route never prints. Parsing lives in ``_bridge_output.py``, which
    ``tests/unit/test_bridge_output_parsing.py`` covers without the live harness.
    """
    problem = converged_pass_problem(cp.stdout, cp.stderr)
    assert problem is None, f"{what}: {problem}\n{cp.stdout}\n--stderr--\n{cp.stderr}"


def _assert_wrote_nothing(cp: subprocess.CompletedProcess[str], *, what: str) -> None:
    """Idempotence, read off the reconciler's own counters.

    NOT just ``BRIDGE_STATE: converged``: ``__main__.py`` classifies a pass that applied N
    mutations as CONVERGED too, so that line means "settled", not "wrote nothing". Asserting
    on it alone would leave this cell green while a pass thrashed the remote every run — the
    exact failure it exists to catch. Zero-write evidence therefore comes from the
    unconditional ``RECON:`` stderr telemetry: both differ totals zero
    (``run_differs.py``) and no per-mutation ``batch_outcome`` line (``applier.py``).
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
    """A DC deployment is still the ``jira`` FAMILY: tickets it creates carry the shared
    ``jira`` creation channel and ``jira-`` local-id prefix, with the DEPLOYMENT
    distinguished by ``RemoteRef.instance`` — not by a second creation channel. Nothing in
    the store vocabulary forks per deployment, which is why this epic needs no migration.
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

    # Match on the DERIVED local id, exactly — NOT `remote_key in json.dumps(ticket)`.
    #
    # That substring form could never pass, and it hid this test's real verdict for the
    # entire life of the epic (bug 23ed). Two independent reasons: `_jira_key_to_local_id`
    # LOWERCASES (`inbound_translate.py:120-124` — "RBJ…-1" -> "jira-rbj…-1") and the `in`
    # test is case-sensitive; and the inbound CREATE payload
    # (`apply_inbound_records.py:183-212`) carries no raw Jira key at all — the key appears
    # only as a TITLE FALLBACK, unused whenever the issue has a summary, as it does here.
    # So a perfectly-created ticket produced a red, and a broken one would have too. A blob
    # scan is also the wrong shape regardless: it can match an incidental occurrence in any
    # field and assert nothing about correspondence.
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
