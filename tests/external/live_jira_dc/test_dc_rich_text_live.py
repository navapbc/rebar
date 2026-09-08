"""Exercise Jira Data Center's real rich-text renderer (story 3289, epic 708d).

The shared live harness complements the offline pandoc proxy. It verifies rendered
heading, bold, and code HTML; zero writes on a second pass; and local-wins arbitration
plus a deduped conflict alert after a converged baseline and both-sides edit.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from _bridge_output import converged_pass_problem, wrote_nothing_problem
from _child_diag import assert_child_ran_clean
from _dc_support import live_jira_ready
from _dc_support import run_bridge as _run_bridge
from _dc_support import skip_no_extra as _skip_no_extra
from _dc_support import skip_no_harness as _skip

# Re-export under the name used to mark live modules and reject collected-but-all-skipped runs.
_live_jira_ready = live_jira_ready


def _uniq(prefix: str) -> str:
    """A token no prior run can have written, so an oracle cannot pass on a stale read."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _rich_markdown(heading: str, bold: str, code: str) -> str:
    """Build Markdown heading/bold text plus an exact Jira ``{code}`` macro.

    The codec converts the Markdown to wiki; a fenced block would remain literal and
    would not exercise Jira's rendered ``<pre>`` panel.
    """
    return f"# {heading}\n\nA paragraph with **{bold}** emphasis.\n\n{{code}}\n{code}\n{{code}}\n"


# Scoped ``run_bridge ... --only`` uses f449's direct-GET snapshot overlay, avoiding JQL
# index waits after writes. Only compatibility ``run_reconcile`` uses ``--filter-local-ids``.


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
) -> None:
    """Set the local description and run a scoped writing pass to convergence.

    ``--only`` is required because the binding-scrubbed copy would otherwise create every
    ticket. REST writes and f449's direct-GET overlay remove any post-push index wait.
    """
    import rebar

    rebar.edit_ticket(local_id, repo_root=repo, description=description)
    cp = _run_bridge(repo, "sync", only=f"{local_id},{key}", max_changes=10)
    assert_child_ran_clean(cp, what=f"{what} pass")
    problem = converged_pass_problem(cp.stdout, cp.stderr)
    assert problem is None, f"{what}: {problem}\n{cp.stdout}\n--stderr--\n{cp.stderr}"


@_skip
@_skip_no_extra
def test_live_rich_text_direct_wire_probe(
    dc_transport: Any,
    jira_dc_project: str,
    bound_dc_issue: Any,
    dc_request: Any,
) -> None:
    """Isolate Jira DC's renderer from reconciliation (reckless-diabolic-kob).

    PUT the exact outbound ``WikiTextCodec`` wire, then read both raw description and
    rendered HTML. Sent/raw/rendered diagnostics distinguish applier faults from DC
    storage or rendering faults.
    """
    from rebar_reconciler.adapters.jira_family.rich_text import WikiTextCodec

    _local_id, key = bound_dc_issue
    dc_transport.project = jira_dc_project

    heading = _uniq("graywolf-probe-heading")
    bold = _uniq("graywolf-probe-bold")
    code = _uniq("graywolf_probe_code")
    md = _rich_markdown(heading, bold, code)
    codec = WikiTextCodec(rich=True)
    wire = codec.normalize_outbound(codec.fit_outbound(md))

    dc_transport.update_issue(key, description=wire)

    raw = (dc_transport.get_issue_by_rest(key).get("fields") or {}).get("description")
    status, payload = dc_request(f"/rest/api/2/issue/{key}?expand=renderedFields")
    rendered = (
        (payload.get("renderedFields") or {}).get("description")
        if isinstance(payload, dict)
        else None
    )

    diag = (
        f"\n--- DIRECT WIRE PROBE {key} ---"
        f"\nSENT wire:  {wire!r}"
        f"\nRAW stored: {raw!r}"
        f"\nRENDERED:   {rendered!r}"
        f"\n(renderedFields GET status {status})\n"
    )

    # Check rendered HTML before raw equality so benign normalization cannot hide renderer
    # evidence; every failure includes sent, raw, rendered, and status values.
    lowered = (rendered or "").lower()
    assert heading in (rendered or "") and "<h1" in lowered, (
        f"the heading did not render to an <h1> element on a direct wire PUT.{diag}"
    )
    assert bold in (rendered or "") and ("<b>" in lowered or "<strong>" in lowered), (
        f"the bold span did not render to <b>/<strong> on a direct wire PUT.{diag}"
    )
    assert code in (rendered or "") and ("<pre" in lowered or 'class="code' in lowered), (
        f"the code block did not render to a code macro on a direct wire PUT.{diag}"
    )
    # A non-200 renderedFields GET would have yielded ``rendered=None`` above and failed the
    # render checks with a misleading "did not render" message; assert it explicitly so a
    # transport/auth fault is named as such.
    assert status == 200, f"renderedFields GET did not return 200.{diag}"
    # RAW round-trip LAST, with the SAME rstrip tolerance the outbound differ uses
    # (``_text_matches``): DC storing the wiki verbatim modulo trailing whitespace is fine;
    # an internal divergence (what would re-emit forever) is not.
    assert (raw or "").rstrip() == wire.rstrip(), (
        f"Jira DC did not store the wiki wire (beyond trailing whitespace).{diag}"
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
    """Render unique heading/bold/code tokens, then prove echo safety.

    Jira must return the tokens inside heading, bold, and preformatted HTML. An immediate
    second pass must report zero writes, not merely convergence.
    """
    local_id, key = bound_dc_issue
    dc_transport.project = jira_dc_project

    # Enable the DC cutover in the ``run_bridge`` child environment; otherwise Markdown is
    # sent unchanged and cannot render the expected HTML (story 3388).
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
    )

    html = _rendered_description_html(dc_request, key)
    # DIAGNOSTIC (bug reckless-diabolic-kob): also read the RAW stored description the
    # reconcile push landed, so a failure shows whether the applier stored the wiki wire,
    # an empty value, or something Jira mangled — the complement to the direct-wire probe.
    raw_stored = (dc_transport.get_issue_by_rest(key).get("fields") or {}).get("description")
    _diag = f"\nRAW stored description after reconcile push: {raw_stored!r}"
    lowered = html.lower()
    assert heading in html and "<h1" in lowered, (
        f"the heading did not render to an <h1> element.{_diag}\nrendered HTML:\n{html}"
    )
    assert bold in html and ("<b>" in lowered or "<strong>" in lowered), (
        f"the bold span did not render to a <b>/<strong> element.{_diag}\nrendered HTML:\n{html}"
    )
    assert code in html and ("<pre" in lowered or 'class="code' in lowered), (
        f"the code block did not render to a code macro (<pre>/code panel).{_diag}\n"
        f"rendered HTML:\n{html}"
    )

    # ECHO-SAFETY — the immediately-following pass must write nothing.
    second = _run_bridge(dc_store_copy_repo, "sync", only=f"{local_id},{key}", max_changes=10)
    assert_child_ran_clean(second, what="echo pass")
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
    """Keep local rich text and record a genuine both-sides conflict.

    After a converged baseline, edit the description locally and remotely before the next
    pass. The local body must win and a deduped
    ``outbound-field-conflict:<key>:description`` alert must preserve the remote evidence.
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
    )
    settle = _run_bridge(dc_store_copy_repo, "sync", only=f"{local_id},{key}", max_changes=10)
    assert_child_ran_clean(settle, what="baseline settle pass")
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

    # (3) THE PASS — local-wins emit + recorded conflict. The scoped pass direct-GETs the key
    # (bug f449 overlay), so it sees the Jira-side edit lag-free — no search-index wait needed.
    cp = _run_bridge(dc_store_copy_repo, "sync", only=f"{local_id},{key}", max_changes=10)
    assert_child_ran_clean(cp, what="conflict pass")
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
