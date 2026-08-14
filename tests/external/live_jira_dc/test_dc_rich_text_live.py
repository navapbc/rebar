"""Live rich-text fidelity against a real Jira Data Center instance (story 3289, epic 708d).

The offline DC codec suite validates the wire form against pandoc's jira reader as a
PROXY. What no offline test can show is what Jira's OWN wiki renderer does with that wire
form, or how the state-based echo-safety and settled-conflict arbitration (stories 5c0e +
3388) behave against the real renderer. This module closes that residual gap by extending
the existing Dockerized DC harness (it reuses the same `conftest`/`_dc_fixtures` fixtures,
the session-scoped PAT, and the absent-harness / absent-extra skip markers), so it SKIPS
cleanly wherever the harness or the `jira` extra is absent and only asserts on a live
instance.

Two live claims are proven here, each in its own test:

  * RENDERED HTML, not wiki source. `test_live_rich_text_renders_and_echo_is_safe` pushes a
    representative rich body through the real API and reads it back through
    ``?expand=renderedFields`` — the HTML Jira actually interprets — asserting a heading
    (`h1.`→``<h1>``), bold (`*foo*`→``<b>``), and a code macro (`{code}`→``<pre>``) all
    rendered. No `renderedFields` assertion exists elsewhere in the harness, so this is a
    genuinely new fidelity check rather than a re-read of the wiki source.

  * STATE-BASED ECHO-SAFETY + SETTLED CONFLICT. The same test runs a SECOND reconcile pass
    and asserts it re-pushes nothing (the landed rich body decodes to a value the differ
    recognizes as unchanged — 3388's once-only-upgrade-then-converge holding against the
    real renderer). `test_live_rich_text_both_sides_conflict_keeps_local` then proves 3388's
    settled local-wins arbitration under the precondition policy actually requires: a
    CONVERGED baseline, then a **both-sides** edit — rebar-side AND Jira-side to the
    description — so the next pass keeps rebar's body (local-wins) AND records the deduped
    ``outbound-field-conflict:<key>:description`` bridge alert. The remote edit is surfaced
    through that alert, never silently destroyed; this mirrors 3388's
    ``test_concurrent_conflict_alerts`` precondition (``local != baseline`` AND
    ``remote != baseline``) live, not a novel last-writer clock.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _bridge_output import converged_pass_problem, wrote_nothing_problem
from _dc_support import live_jira_ready
from _dc_support import run_bridge as _run_bridge
from _dc_support import skip_no_extra as _skip_no_extra
from _dc_support import skip_no_harness as _skip

# THE ALL-SKIP CANARY KEYS ON THIS NAME. `tests/external/conftest.py` applies the `jira_live`
# marker only to modules defining a module-level `_live_jira_ready`, and the canary then fails
# a run in which live tests were COLLECTED but none EXECUTED — so a silent all-skip of the
# evidence this story carries cannot pass as green. Re-exported under the name the canary reads.
_live_jira_ready = live_jira_ready


def _uniq(prefix: str) -> str:
    """A token no prior run can have written, so an oracle cannot pass on a stale read."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _rich_markdown(heading: str, bold: str, code: str) -> str:
    """A representative rich body: a level-1 heading, a bold span, and a code macro.

    The heading and the bold span are authored as Markdown (``# ``, ``**foo**``); the
    DC rich codec (story 271c's renderer, gated by the 3388 cutover) renders them to Jira
    wiki (``h1.`` / ``*bold*``), which the real renderer then turns into HTML — the thing
    this module asserts on. The code fragment is authored as a Jira ``{code}`` macro
    directly: the renderer LOCKS code verbatim (a Markdown fenced block passes through as
    literal back-ticks, never a macro — ``wiki_render`` classifies ``{code}``/fences as
    ``_EXACT``), so the only wire that yields a rendered ``<pre>`` code panel is a real
    ``{code}`` block, which the codec passes through unchanged for Jira to render.
    """
    return f"# {heading}\n\nA paragraph with **{bold}** emphasis.\n\n{{code}}\n{code}\n{{code}}\n"


def _wait_until_search_reflects(
    transport: Any,
    project: str,
    key: str,
    predicate: Callable[[dict[str, Any]], bool],
    what: str,
    timeout: float = 90.0,
) -> None:
    """Block until a JQL SEARCH returns ``key`` in a state satisfying ``predicate``.

    The reconcile pass reads remote fields off a JQL search, and Jira's Lucene index is
    eventually consistent, so a field written a moment ago may not be visible to the pass
    yet. Waiting for the SEARCH (not a direct GET) to reflect the change is what keeps a
    timing artefact from being reported as a bridge defect — the same hazard the sibling
    ``test_dc_mutations`` inbound cells guard against.
    """
    deadline = time.monotonic() + timeout
    attempts = 0
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        attempts += 1
        for hit in transport.search_issues(f'project = "{project}" AND key = "{key}"'):
            if hit.get("key") == key:
                last = hit
                if predicate(hit):
                    return
        time.sleep(2.0)
    raise AssertionError(
        f"the index never reflected {what} on {key} within {timeout:.0f}s ({attempts} "
        f"attempts). This is NOT a bridge defect — the write succeeded, the SEARCH cannot see "
        f"it yet. Last indexed fields: {(last or {}).get('fields')!r}"
    )


def _rendered_description_html(dc_request: Any, key: str) -> str:
    """The rendered-HTML ``description`` for ``key`` via ``?expand=renderedFields``.

    This is a direct authenticated GET (immediately consistent — no index lag), and it is
    the WHOLE point of this module: ``renderedFields.description`` is the HTML Jira produced
    from the stored wiki source, i.e. what a human actually sees, not the wiki markup the
    wire carried.
    """
    status, body = dc_request(f"/rest/api/2/issue/{key}?expand=renderedFields")
    assert status == 200, f"GET renderedFields for {key} returned {status}: {body!r}"
    assert isinstance(body, dict), f"unexpected renderedFields payload for {key}: {body!r}"
    rendered = (body.get("renderedFields") or {}).get("description")
    assert isinstance(rendered, str), (
        f"{key} has no rendered description HTML: renderedFields is {body.get('renderedFields')!r}"
    )
    return rendered


def _push_and_converge(
    repo: Path,
    local_id: str,
    key: str,
    description: str,
    *,
    what: str,
    transport: Any,
    project: str,
    visible_token: str,
) -> None:
    """Set the local ``description``, run a scoped writing pass, and wait for the index.

    Scoping (``--only local_id,key``) is MANDATORY for writing passes here: the store copy
    is binding-scrubbed, so an unscoped pass would route the whole copied store down the
    CREATE path. Asserts the pass settled (no traceback, ``BRIDGE_STATE: converged``).

    After the push, blocks until a JQL SEARCH reflects ``visible_token`` in the description.
    The push writes over REST (immediately visible to a direct GET) but the NEXT pass reads
    remote fields off the eventually-consistent JQL search, so a follow-up pass run before
    the index caught up would diff against a stale (pre-push) remote and could churn — the
    index-lag hazard the sibling inbound cells guard against. Waiting here makes the echo /
    settle pass that follows deterministic.
    """
    import rebar

    rebar.edit_ticket(local_id, repo_root=repo, description=description)
    cp = _run_bridge(repo, "sync", only=f"{local_id},{key}", max_changes=10)
    assert "Traceback" not in cp.stderr, f"{what} pass raised:\n{cp.stderr[-2000:]}"
    problem = converged_pass_problem(cp.stdout, cp.stderr)
    assert problem is None, f"{what}: {problem}\n{cp.stdout}\n--stderr--\n{cp.stderr}"
    _wait_until_search_reflects(
        transport,
        project,
        key,
        lambda h: visible_token in ((h.get("fields") or {}).get("description") or ""),
        f"the pushed body ({what})",
    )


@_skip
@_skip_no_extra
def test_live_rich_text_renders_and_echo_is_safe(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    bound_dc_issue: Any,
    dc_request: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 + AC2 (echo half): a pushed rich body renders to HTML, and a second pass re-pushes
    nothing.

    RENDERED HTML: after pushing a heading/bold/code body, the ``renderedFields`` HTML must
    carry an ``<h1>`` (heading), a ``<b>`` (bold), and a ``<pre>`` (the code macro), each
    around the unique token this run wrote — so the assertion cannot pass on a stale document
    or on wiki source that never rendered.

    ECHO-SAFETY: a second reconcile pass immediately after must be a NO-OP. The landed rich
    body decodes to a value the differ recognizes as unchanged (5c0e render robustness +
    3388 upgrade-then-converge), so nothing is re-pushed. A pass that re-emitted every run
    would still look green on a single run while thrashing the remote — hence the zero-write
    assertion off the reconciler's own counters, not merely ``converged``.
    """
    local_id, key = bound_dc_issue
    dc_transport.project = jira_dc_project

    # Enable the DC rich-text cutover (story 3388) for the reconcile subprocesses this test
    # spawns. The cutover ships OFF (``reconciler.rich_text_cutover`` defaults to ``off``), so
    # ``WikiTextCodec.to_wire`` is the IDENTITY by default and a rich body would reach Jira as
    # raw Markdown (``# x`` renders as an ordered list, not ``<h1>``). This is exactly the
    # residual risk graywolf exists to prove is CLOSED when the flag is on: with the cutover
    # enabled the codec renders Markdown to wiki markup and Jira produces the expected HTML.
    # ``run_bridge`` builds the subprocess env from ``os.environ`` (via ``engine_env``), so the
    # canonical env override reaches ``cutover_clients()`` in the child; ``monkeypatch`` reverts
    # it after the test.
    monkeypatch.setenv("REBAR_RECONCILER_RICH_TEXT_CUTOVER", "dc")

    heading = _uniq("graywolf-heading")
    bold = _uniq("graywolf-bold")
    code = _uniq("graywolf_code")
    _push_and_converge(
        dc_store_copy_repo,
        local_id,
        key,
        _rich_markdown(heading, bold, code),
        what="rich-body push",
        transport=dc_transport,
        project=jira_dc_project,
        visible_token=heading,
    )

    html = _rendered_description_html(dc_request, key)
    lowered = html.lower()
    assert heading in html and "<h1" in lowered, (
        f"the heading did not render to an <h1> element. rendered HTML:\n{html}"
    )
    assert bold in html and ("<b>" in lowered or "<strong>" in lowered), (
        f"the bold span did not render to a <b>/<strong> element. rendered HTML:\n{html}"
    )
    assert code in html and ("<pre" in lowered or 'class="code' in lowered), (
        f"the code block did not render to a code macro (<pre>/code panel). rendered HTML:\n{html}"
    )

    # ECHO-SAFETY — the immediately-following pass must write nothing.
    second = _run_bridge(dc_store_copy_repo, "sync", only=f"{local_id},{key}", max_changes=10)
    assert "Traceback" not in second.stderr, f"echo pass raised:\n{second.stderr[-2000:]}"
    problem = wrote_nothing_problem(second.stdout, second.stderr)
    assert problem is None, (
        "the second pass re-pushed the rich body — echo-safety does not hold against the real "
        f"renderer ({problem}). This is the once-only-upgrade-then-converge guarantee (3388) "
        f"regressing:\n{second.stdout}\n--stderr--\n{second.stderr}"
    )


@_skip
@_skip_no_extra
def test_live_rich_text_both_sides_conflict_keeps_local(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    jira_dc_project: str,
    bound_dc_issue: Any,
) -> None:
    """AC2 (conflict half): a both-sides edit keeps rebar's body AND records the conflict.

    The ``outbound-field-conflict`` alert fires ONLY on a genuine both-sides divergence
    (``local != baseline`` AND ``remote != baseline``); a bare Jira-side edit with local
    unchanged is Jira-wins inbound with NO conflict, so this test establishes the precondition
    3388's ``test_concurrent_conflict_alerts`` requires:

      1. push a rich body and let a pass converge, so a baseline is recorded;
      2. make a REBAR-side edit to the description AND a JIRA-side edit to the same field
         before the next pass;
      3. run the pass and assert it emits rebar's body (LOCAL-WINS — the rebar token is on
         the DC issue, the Jira token is gone) and records the deduped
         ``outbound-field-conflict:<key>:description`` bridge alert (the remote edit is
         surfaced, never silently destroyed).
    """
    from rebar_reconciler import alert_store

    local_id, key = bound_dc_issue
    dc_transport.project = jira_dc_project

    # (1) CONVERGED BASELINE — push a rich body, then a second pass settles the baseline so
    # the subsequent local edit is measured against a quiet state, not a mid-upgrade one.
    heading = _uniq("graywolf-cxheading")
    base_body = _rich_markdown(heading, _uniq("graywolf-cxbold"), _uniq("graywolf_cxcode"))
    _push_and_converge(
        dc_store_copy_repo,
        local_id,
        key,
        base_body,
        what="baseline push",
        transport=dc_transport,
        project=jira_dc_project,
        visible_token=heading,
    )
    settle = _run_bridge(dc_store_copy_repo, "sync", only=f"{local_id},{key}", max_changes=10)
    assert "Traceback" not in settle.stderr, (
        f"baseline settle pass raised:\n{settle.stderr[-2000:]}"
    )
    assert wrote_nothing_problem(settle.stdout, settle.stderr) is None, (
        "the baseline did not settle before the conflict was staged — a non-quiet pre-state "
        f"would confound the both-sides precondition:\n{settle.stdout}\n--stderr--\n{settle.stderr}"
    )

    # (2) BOTH-SIDES EDIT — rebar-side and Jira-side both diverge from the settled baseline.
    import rebar

    rebar_token = _uniq("graywolf-rebar-wins")
    rebar.edit_ticket(
        local_id,
        repo_root=dc_store_copy_repo,
        description=f"{base_body}\nrebar edit {rebar_token}\n",
    )
    jira_token = _uniq("graywolf-jira-side")
    dc_transport.update_issue(key, description=f"{base_body}\njira edit {jira_token}\n")
    _wait_until_search_reflects(
        dc_transport,
        jira_dc_project,
        key,
        lambda h: jira_token in ((h.get("fields") or {}).get("description") or ""),
        "the Jira-side description edit",
    )

    # (3) THE PASS — local-wins emit + recorded conflict.
    cp = _run_bridge(dc_store_copy_repo, "sync", only=f"{local_id},{key}", max_changes=10)
    assert "Traceback" not in cp.stderr, f"conflict pass raised:\n{cp.stderr[-2000:]}"
    assert converged_pass_problem(cp.stdout, cp.stderr) is None, (
        f"the conflict pass did not settle:\n{cp.stdout}\n--stderr--\n{cp.stderr}"
    )

    landed = (dc_transport.get_issue_by_rest(key).get("fields") or {}).get("description") or ""
    assert rebar_token in landed, (
        f"LOCAL-WINS did not hold: rebar's edit ({rebar_token!r}) is not on the DC issue. "
        f"DC description is {landed!r}"
    )
    assert jira_token not in landed, (
        f"the concurrent Jira edit ({jira_token!r}) survived on the DC issue, so rebar did not "
        f"win the field. DC description is {landed!r}"
    )
    assert alert_store.is_deduped(
        f"outbound-field-conflict:{key}:description", repo_root=dc_store_copy_repo
    ), (
        "the both-sides conflict was NOT recorded as an outbound-field-conflict bridge alert, "
        "so the overwritten remote edit was silently destroyed rather than surfaced. This is "
        "3388's settled-conflict guarantee regressing."
    )
