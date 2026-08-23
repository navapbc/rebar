"""HELD-OUT enumerated 429 sweep for the Cloud (ACLI) adapter — story 2127.

DC gained a per-member 429 sweep in epic e369 (``test_dc_rate_limit_retry_heldout.py``);
Cloud never had one. This module ports the PROPERTY, not the test: DC has a single
``_with_connection_retry`` choke point, whereas Cloud's mutations traverse FOUR distinct
429 behaviours across two levels, and TWO of them retry a 429 on purpose. So a single
attempt-count assertion is meaningless unless it names the BOUNDARY it measures at — this
module asserts a PER-LAYER property, enumerated per member (an ellipsis is how the next
mutation silently inherits a retry).

The four Cloud 429 behaviours (verified against the tree, current line numbers):

  * DIRECT pooled REST — ``AcliRestMixin._rest_urlopen_with_retry`` (acli_rest.py:36) does
    NOT retry ``urllib.error.HTTPError`` (its docstring, acli_rest.py:50): exactly ONE
    attempt.
  * BARE ``urllib.request.urlopen`` — ``acli_cli_ops.update_priority`` (acli_cli_ops.py:396,
    urlopen at :437) bypasses ``_rest_urlopen_with_retry`` entirely, so it has no retry of
    its own either: exactly ONE attempt.
  * ACLI SUBPROCESS — ``acli_subprocess._run_acli`` (acli_subprocess.py) DELIBERATELY
    retries a 429 via ``_rate_limit_backoff`` honouring ``Retry-After``
    (``test_run_acli_429_retries_with_rate_limit_backoff`` pins this): a BOUNDED retry, NOT
    one attempt.
  * DISPATCH WRAPPER — ``dispatch_one._call_with_retry`` (dispatch_one.py:71) adds a 429
    retry ON TOP (its 429/``Retry-After`` branch, dispatch_one.py:115-118), honouring
    ``Retry-After``: a BOUNDED retry, NOT one attempt. The explicitly NON-retrying apply
    phases (``dispatch_apply_phases._update_one_apply_reporter`` :96 /
    ``_update_one_dispatch_comments`` :147) do the opposite: one attempt.

============================================================================================
PER-MEMBER CLASSIFICATION TABLE (the AC1 repo artifact; mirrored onto ticket 2127).
Two axes: TRANSPORT SEAM (subprocess | pooled-REST | bare-urlopen) and DISPATCH LEVEL
(retrying ``_call_with_retry`` | non-retrying ``dispatch_apply_phases`` | none / direct).
"Dup-write safe?" is the duplicate-write assessment (AC7) for members that reach a mutation
through a KNOWN deliberate retry — the authoritative repo artifact, not a tracker-only note.

Per member — ``seam`` / ``dispatch level`` / ``429 attempts`` / ``Dup-write safe? [reason]``:

  create_issue
    subprocess / _call_with_retry (create_one) / bounded retry
    Dup-safe: YES — JQL rebar-id dedup in create_one precedes the write, so a retried
    create cannot duplicate.
  update_issue (field-edit leg)
    subprocess / _call_with_retry / bounded retry
    Dup-safe: YES — ``workitem edit`` is idempotent (last-write-wins on the same fields).
  update_issue (priority leg)
    bare-urlopen / _call_with_retry / one attempt
    Dup-safe: N/A — bare urlopen never retries a 429 itself.
  update_issue (status leg)
    pooled-REST (transition) / _call_with_retry / one attempt
    Dup-safe: N/A — REST leg one-attempt; transition is idempotent anyway (target state).
  add_comment
    subprocess / non-retrying (dispatch_comments) / one attempt
    Dup-safe: N/A — single-attempt by design (9622): a comment has no idempotency key, so
    it is NOT retried.
  transition_issue_by_name
    pooled-REST / _call_with_retry / one attempt
    Dup-safe: N/A — REST one-attempt; POST /transitions idempotent (moves to a target state).
  unassign_issue
    pooled-REST / _call_with_retry / one attempt
    Dup-safe: N/A — one attempt; idempotent (accountId=null).
  set_parent
    pooled-REST / _call_with_retry / one attempt
    Dup-safe: N/A — one attempt; idempotent (sets parent key).
  delete_issue
    subprocess / direct (applier) / bounded retry
    Dup-safe: YES — delete is idempotent (404-on-gone treated as success by delete_issue's
    own handler).
  set_issue_property / set_entity_property
    pooled-REST / _call_with_retry / one attempt
    Dup-safe: N/A — one attempt; PUT property is idempotent.
  set_reporter
    pooled-REST / non-retrying (apply_reporter) / one attempt
    Dup-safe: N/A — one attempt; soft-degrades on HTTPError.
  update_priority (graph method)
    pooled-REST / _call_with_retry / one attempt
    Dup-safe: N/A — one attempt; PUT priority idempotent.
  update_issuetype
    pooled-REST / _call_with_retry / one attempt
    Dup-safe: N/A — one attempt; PUT issuetype idempotent.
  delete_comment
    pooled-REST / _call_with_retry / one attempt
    Dup-safe: N/A — one attempt; DELETE idempotent.
  add_label / _add_label_impl
    subprocess / _call_with_retry / bounded retry
    Dup-safe: YES — ``labelsToAdd`` is additive + idempotent (re-adding a label is a no-op).
  remove_label / _remove_label_impl
    subprocess / _call_with_retry / bounded retry
    Dup-safe: YES — ``labelsToRemove`` is target-specific + idempotent (removing an absent
    label no-ops).
  update_comment
    subprocess / _call_with_retry / bounded retry
    Dup-safe: YES — comment UPDATE targets a fixed comment id (last-write-wins), not append.
  set_relationship
    subprocess / _call_with_retry / bounded retry
    Dup-safe: PARTIAL — a retried ``link create`` CAN create a duplicate link; the differ
    de-dups links by (type, other_key) on the next pass, so a dup is self-healing but
    transiently visible. Tracked: no new bug — pre-existing + differ-guarded.
  delete_issue_link
    subprocess / _call_with_retry / bounded retry
    Dup-safe: YES — delete-by-id idempotent (404 == gone).

NON-MUTATING names from the Step-0 enumeration (reads / helpers / ctor), each excluded with
a reason — see ``test_enumeration_completeness_and_dc_crosscheck`` below, which asserts every
name the Step-0 command returns is accounted for.

Step-0 enumeration command (AC2 completeness; raw output recorded in the test below):

    git grep -nE "^(    )?def [_a-z]" -- \
      src/rebar/_engine/rebar_reconciler/adapters/jira/acli.py \
      src/rebar/_engine/rebar_reconciler/adapters/jira/acli_graph.py \
      src/rebar/_engine/rebar_reconciler/adapters/jira/acli_rest.py \
      src/rebar/_engine/rebar_reconciler/adapters/jira/acli_cli_ops.py
============================================================================================

MUTATION CHECK (AC final bullet), recorded RED/GREEN in the change description:
  * pooled-REST / bare-urlopen one-attempt sweep: make one member retry once at the urlopen
    boundary → the ``== 1`` attempt assertion goes RED.
  * subprocess-seam bounded-retry sweep: cap ``_run_acli``'s attempt loop at one (or re-raise
    on a 429 exit without looping) → the retry/attempt-count assertion goes RED.
  * dispatch-wrapped path: unbound the retry in ``_call_with_retry`` → the bounded-retry
    assertion goes RED; and drop ``Retry-After`` honouring → the delay assertion goes RED.
  * non-retrying apply-phases sweep: add a second transport call inside
    ``_update_one_apply_reporter`` / ``_update_one_dispatch_comments`` → the ``== 1``
    assertion goes RED.
"""

from __future__ import annotations

import ast
import email.message
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

# Engine is on sys.path via the package conftest.
from rebar_reconciler.adapters.jira import acli as acli_mod
from rebar_reconciler.adapters.jira import acli_cli_ops, acli_subprocess
from rebar_reconciler.dispatch_apply_phases import (
    _update_one_apply_reporter,
    _update_one_dispatch_comments,
)


def _http_429(retry_after: str | None = "1") -> urllib.error.HTTPError:
    hdrs = None
    if retry_after is not None:
        hdrs = email.message.Message()
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        url="https://example.atlassian.net/rest/api/3/issue/DIG-1",
        code=429,
        msg="Too Many Requests",
        hdrs=hdrs,  # type: ignore[arg-type]
        fp=None,
    )


def _client(**creds: str) -> acli_mod.AcliClient:
    base = {"jira_url": "https://example.atlassian.net", "user": "u", "api_token": "t"}
    base.update(creds)
    return acli_mod.AcliClient(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. POOLED-REST + BARE-urlopen: EXACTLY ONE attempt under a 429.
#    Observed at the shared process-wide ``urllib.request.urlopen`` (a singleton —
#    every acli_* module resolves it through the same ``urllib.request`` object;
#    see mutate/test_acli_cross_mixin_binding.py). One case PER MEMBER.
# ---------------------------------------------------------------------------

# Each entry: name -> callable(client) that drives exactly that member's transport write.
_POOLED_REST_MEMBERS: dict[str, Callable[[acli_mod.AcliClient], Any]] = {
    "set_issue_property": lambda c: c.set_issue_property("DIG-1", "rebar_id", "abc"),
    "set_entity_property": lambda c: c.set_entity_property("DIG-1", "rebar_id", "abc"),
    "set_reporter": lambda c: c.set_reporter("DIG-1", "acct-1"),
    "update_priority": lambda c: c.update_priority("DIG-1", "High"),
    "update_issuetype": lambda c: c.update_issuetype("DIG-1", "Bug"),
    "set_parent": lambda c: c.set_parent("DIG-1", "DIG-2"),
    "unassign_issue": lambda c: c.unassign_issue("DIG-1"),
    "delete_comment": lambda c: c.delete_comment("DIG-1", "10001"),
    # transition_issue_by_name's FIRST REST call is the GET /transitions read; a 429
    # there is the observable boundary and must not retry.
    "transition_issue_by_name": lambda c: c.transition_issue_by_name("DIG-1", "Done"),
}


@pytest.mark.parametrize("member", sorted(_POOLED_REST_MEMBERS))
def test_pooled_rest_member_makes_exactly_one_attempt_on_429(
    member: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def _fake_urlopen(*_a: Any, **_k: Any) -> Any:
        calls.append(1)
        raise _http_429()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    client = _client()
    with pytest.raises(urllib.error.HTTPError) as caught:
        _POOLED_REST_MEMBERS[member](client)
    assert caught.value.code == 429
    assert len(calls) == 1, (
        f"{member} issued {len(calls)} urlopen attempts under a 429 — a pooled-REST "
        f"mutation must make EXACTLY ONE (acli_rest.py:50 does not retry HTTPError). "
        f"More than one reintroduces the duplicate-write class 21fc fixed"
    )


def test_bare_urlopen_update_priority_makes_exactly_one_attempt_on_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``acli_cli_ops.update_priority`` (the free function) writes via BARE
    ``urllib.request.urlopen`` (acli_cli_ops.py:437), bypassing
    ``_rest_urlopen_with_retry`` — so it has no retry of its own: exactly ONE attempt."""
    calls: list[int] = []

    def _fake_urlopen(*_a: Any, **_k: Any) -> Any:
        calls.append(1)
        raise _http_429()

    # The free function resolves creds via resolve_jira_settings; give it all three so it
    # reaches the urlopen write rather than warning-and-skipping on a missing credential.
    monkeypatch.setattr(
        acli_subprocess,
        "resolve_jira_settings",
        lambda **_k: acli_subprocess.JiraSettings(
            url="https://example.atlassian.net", user="u", project="DIG", api_token="t"
        ),
    )
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    with pytest.raises(urllib.error.HTTPError) as caught:
        acli_cli_ops.update_priority("DIG-1", "High")
    assert caught.value.code == 429
    assert len(calls) == 1, (
        f"acli_cli_ops.update_priority issued {len(calls)} bare-urlopen attempts under a "
        f"429 — it bypasses _rest_urlopen_with_retry and must make EXACTLY ONE"
    )


# ---------------------------------------------------------------------------
# 2. DISPATCH-WRAPPED path: reached through ``dispatch_one._call_with_retry``, a 429 IS
#    retried by design (dispatch_one.py:115-118). The assertion here is a BOUNDED retry
#    honouring ``Retry-After`` — NOT one attempt.
# ---------------------------------------------------------------------------


def test_dispatch_wrapped_429_is_a_bounded_retry_honouring_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rebar_reconciler import dispatch_one

    slept: list[float] = []
    monkeypatch.setattr(dispatch_one.time, "sleep", lambda s: slept.append(s))

    calls: list[int] = []

    def _twice_then_ok() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise _http_429(retry_after="5")
        return "ok"

    assert dispatch_one._call_with_retry(_twice_then_ok, max_retries=3) == "ok"
    assert len(calls) == 3, "the dispatch wrapper did not retry an opted-in 429"
    # Retry-After=5 honoured (min(MAX_BACKOFF_S, 5) == 5), not a jittered fallback.
    assert slept == [5.0, 5.0], f"expected two Retry-After(5s) backoffs, got {slept}"


def test_dispatch_wrapped_429_is_bounded_not_infinite(monkeypatch: pytest.MonkeyPatch) -> None:
    """A persistent 429 exhausts a BOUNDED number of retries then re-raises the raw
    HTTPError — the retry is bounded by ``max_retries`` (mutation: unbound the loop)."""
    from rebar_reconciler import dispatch_one

    monkeypatch.setattr(dispatch_one.time, "sleep", lambda _s: None)
    calls: list[int] = []

    def _always_429() -> str:
        calls.append(1)
        raise _http_429(retry_after="1")

    with pytest.raises(urllib.error.HTTPError) as caught:
        dispatch_one._call_with_retry(_always_429, max_retries=3)
    assert caught.value.code == 429
    assert len(calls) == 4, f"expected initial + 3 bounded retries, got {len(calls)} attempts"


# ---------------------------------------------------------------------------
# 3. NON-retrying ``dispatch_apply_phases`` mutations: EXACTLY ONE attempt under a 429,
#    extending ``test_comment_single_attempt.py``'s single-member pin to the sweep.
# ---------------------------------------------------------------------------


def test_apply_reporter_makes_exactly_one_attempt_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_update_one_apply_reporter`` routes reporter through ``client.set_reporter``
    OUTSIDE ``_call_with_retry``; a 429 there is soft-degraded after exactly one attempt."""
    import rebar_reconciler.dispatch_apply_phases as dap

    # Force the reporter to resolve so set_reporter is actually reached.
    monkeypatch.setattr(dap, "_jira_account_id_for", lambda _r: "acct-1")
    monkeypatch.setattr(dap, "_record_reporter_alert", lambda *_a, **_k: None)

    attempts: list[int] = []

    class _Client:
        def set_reporter(self, _key: str, _acct: str) -> None:
            attempts.append(1)
            raise _http_429()

    reporter_fields = {"reporter": "someone"}
    _update_one_apply_reporter(reporter_fields, "DIG-1", _Client())  # type: ignore[arg-type]
    assert len(attempts) == 1, (
        f"set_reporter was attempted {len(attempts)} times under a 429 — the non-retrying "
        f"apply-phase must make EXACTLY ONE attempt"
    )


def test_dispatch_comments_makes_exactly_one_attempt_on_429() -> None:
    """``_update_one_dispatch_comments`` posts comments single-attempt (9622); a 429 is
    recorded to ``comment_errors`` after exactly one attempt, never retried."""
    attempts: list[int] = []

    class _Client:
        def add_comment(self, _key: str, _body: str) -> dict[str, Any]:
            attempts.append(1)
            raise _http_429()

    comment_errors: list[str] = []
    mutation = {"key": "DIG-1", "comments": [{"body": "hello"}]}
    _update_one_dispatch_comments(
        mutation,
        _Client(),
        "DIG-1",
        comment_errors,  # type: ignore[arg-type]
    )
    assert len(attempts) == 1, (
        f"add_comment was attempted {len(attempts)} times under a 429 — the single-attempt "
        f"comment dispatch must make EXACTLY ONE attempt"
    )
    assert len(comment_errors) == 1


# ---------------------------------------------------------------------------
# 4. ACLI-SUBPROCESS seam: each subprocess-seam mutating member inherits ``_run_acli``'s
#    bounded ``Retry-After`` backoff. Driven through the REAL ``_run_acli`` (a fake ``acli``
#    binary emitting a 429 on the first call, success after), observed at the ``_run_acli``
#    boundary via the ``_backoff_sleep`` seam. Asserting ONE attempt here would be WRONG —
#    ``test_run_acli_429_retries_with_rate_limit_backoff`` proves the retry is deliberate.
# ---------------------------------------------------------------------------

# A stateful fake ``acli`` binary: first invocation -> 429 on stderr + exit 1 (drives the
# rate-limit backoff + retry); subsequent invocations -> exit 0 with a shape valid for the
# command (``search`` -> a one-issue list so verify-after-create resolves; ``comment``+
# ``list`` -> a page; else -> ``{"key": "DIG-1"}``).
_FAKE_ACLI_429_THEN_OK = r"""
import sys, os, json
counter = os.environ["FAKE_429_COUNTER"]
n = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(n + 1))
if n == 0:
    sys.stderr.write("ACLI error: HTTP 429 Too Many Requests\nRetry-After: 1\n")
    sys.exit(1)
argv = sys.argv
if "search" in argv:
    sys.stdout.write(json.dumps([{"key": "DIG-1", "summary": "s", "status": "To Do"}]))
elif "comment" in argv and "list" in argv:
    sys.stdout.write(json.dumps({"comments": []}))
else:
    sys.stdout.write(json.dumps({"key": "DIG-1"}))
sys.exit(0)
"""


def _fake_acli_cmd(tmp_path: Path) -> list[str]:
    prog = tmp_path / "fake_acli.py"
    prog.write_text(_FAKE_ACLI_429_THEN_OK)
    return [sys.executable, str(prog)]


# name -> callable(client) driving that subprocess-seam member. A no-creds client keeps
# create_issue's verify-after-create on the deterministic ``get_issue`` (search) path
# rather than a live REST GET.
_SUBPROCESS_MEMBERS: dict[str, Callable[[acli_mod.AcliClient], Any]] = {
    "create_issue": lambda c: c.create_issue({"title": "s", "ticket_type": "task"}),
    "update_issue": lambda c: c.update_issue("DIG-1", summary="s2"),
    "add_comment": lambda c: c.add_comment("DIG-1", "body"),
    "add_label": lambda c: c.add_label("DIG-1", "lbl"),
    "remove_label": lambda c: c.remove_label("DIG-1", "lbl"),
    "update_comment": lambda c: c.update_comment("DIG-1", "10001", "body"),
    "set_relationship": lambda c: c.set_relationship("DIG-1", "DIG-2", "Blocks"),
    "delete_issue": lambda c: c.delete_issue("DIG-1"),
    "delete_issue_link": lambda c: c.delete_issue_link("10001"),
}


@pytest.mark.parametrize("member", sorted(_SUBPROCESS_MEMBERS))
def test_subprocess_member_retries_a_429_with_bounded_retry_after_backoff(
    member: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_429_COUNTER", str(tmp_path / f"n_{member}"))
    delays: list[float] = []
    # Patch the narrow retry-backoff seam (not module-global time.sleep, which also
    # captures CPython's communicate() busy-wait poll sleeps — flaky under load).
    monkeypatch.setattr(acli_subprocess, "_backoff_sleep", lambda s: delays.append(s))

    client = acli_mod.AcliClient(
        jira_url="", user="", api_token="", jira_project="DIG", acli_cmd=_fake_acli_cmd(tmp_path)
    )
    _SUBPROCESS_MEMBERS[member](client)  # must succeed on the retry, not raise

    assert delays == [1.0], (
        f"{member} did not inherit _run_acli's bounded Retry-After backoff: expected exactly "
        f"one Retry-After=1s backoff, got {delays}. A subprocess-seam mutation must retry a "
        f"429 through _run_acli (test_run_acli_429_retries_with_rate_limit_backoff proves the "
        f"retry is deliberate) — asserting one attempt here would be wrong"
    )


# ---------------------------------------------------------------------------
# AC2 completeness: run the Step-0 enumeration command and account for EVERY name it
# returns — either it is a mutating member classified in the table above, or it is marked
# NON-MUTATING here with a reason. Plus the DC cross-check.
# ---------------------------------------------------------------------------

# Every MUTATING member the table classifies (public transport write surface + the two
# private label impls the table names explicitly).
_CLASSIFIED_MUTATING = {
    "transition_issue",  # free func: composes a status change (mutation entry point)
    "update_issue",  # both the free func and the AcliClient method
    "create_issue",  # both acli_cli_ops.create_issue and the AcliClient method
    "add_comment",
    "transition_issue_by_name",
    "unassign_issue",
    "set_parent",
    "delete_issue",
    "add_label",
    "_add_label_impl",
    "remove_label",
    "_remove_label_impl",
    "update_priority",  # graph method + acli_cli_ops free func (bare-urlopen)
    "update_issuetype",
    "update_comment",
    "delete_comment",
    "set_relationship",
    "delete_issue_link",
    "set_issue_property",
    "set_reporter",
    "set_entity_property",
    "_direct_rest_put",
    "_direct_rest_put_raw",
    "_direct_rest_post_raw",
    "_direct_rest_delete",
}

# Every NON-MUTATING name the Step-0 command returns, each with the reason it is excluded.
_NON_MUTATING = {
    "__init__": "constructor — stores credentials, no transport call",
    "_run": "generic subprocess runner used by both reads and writes; not itself a mutation",
    "get_issue": "READ (JQL search / REST GET)",
    "get_issue_by_rest": "READ (REST GET)",
    "search_issues": "READ (JQL search)",
    "invalidate_search_cache": "pure memo evict — no transport call at all",
    "get_server_info": "READ",
    "get_myself": "READ",
    "get_comments": "READ (comment list)",
    "get_parent_map": "READ (bulk parent map)",
    "get_comment_map": "READ (bulk comment map)",
    "get_issuelinks_map": "READ (bulk issuelinks map)",
    "get_issue_links": "READ (REST GET issuelinks)",
    "get_issue_link_types": "READ (link-type catalogue)",
    "get_issue_property": "READ (REST GET property)",
    "get_entity_property": "READ (alias of get_issue_property)",
    "_direct_rest_get": "READ leg (GET); mutating callers are classified by their write leg",
    "_direct_rest_post_json": "READ leg (POST search) used by get_parent_map",
    "validate_assignee_exists": "READ (assignable/search); resolves an accountId, no write",
    "search_user_by_email": "READ (user/search); resolves an accountId, no write",
    "_iter_cursor_pages": "READ helper (pagination)",
    "_parse_acli_comments": "pure parse helper",
    "_parse_comment_created": "pure parse helper (normalizes the create-comment return)",
    "_parse_paginated_comments": "pure parse helper",
    "_verify_created_issue": "READ-after-create verify helper (GET/search)",
    "_extract_parent_key": "pure parse helper",
    "_attach_parent_guarded": "wrapper over set_parent (the write is set_parent, classified)",
    "_create_issue_no_json": "create helper (write leg is create_issue, classified)",
    "_create_from_json_payload": "pure payload builder",
    "_create_issue_from_json": "create helper (write leg is create_issue, classified)",
    "_rest_urlopen_with_retry": "the retry helper itself; the mutating callers are classified",
}

# DC's enumerated mutating set (test_dc_rate_limit_retry_heldout.py). Members DC sweeps that
# Cloud lacks, or vice versa, are recorded with the reason (legitimate asymmetry vs gap).
_DC_SWEPT = {
    "create_issue",
    "update_issue",
    "add_comment",
    "add_label",
    "remove_label",
    "transition_issue_by_name",
    "set_relationship",
    "delete_issue",
    "delete_issue_link",
    "set_parent",
}


_JIRA_ADAPTER_PKG = "src/rebar/_engine/rebar_reconciler/adapters/jira"

# The DECLARED transport surface this sweep is ABOUT (ticket 8a5e ruled that scoping
# correct; ticket 81d5's operator decision — option B — kept it rather than globbing the
# package). Naming it makes the sweep's subject a reviewable constant instead of a literal
# buried inside _step0_names, and gives the anti-drift guard below a denominator to derive
# "everything else in the package" from.
_SWEPT_TRANSPORT_MODULES = (
    f"{_JIRA_ADAPTER_PKG}/acli.py",
    f"{_JIRA_ADAPTER_PKG}/acli_graph.py",
    f"{_JIRA_ADAPTER_PKG}/acli_rest.py",
    f"{_JIRA_ADAPTER_PKG}/acli_cli_ops.py",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _step0_names() -> set[str]:
    repo_root = _repo_root()
    files = list(_SWEPT_TRANSPORT_MODULES)
    out = subprocess.run(
        ["git", "grep", "-nE", r"^(    )?def [_a-z]", "--", *files],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    names: set[str] = set()
    for line in out.splitlines():
        # <file>:<lineno>:<indent>def <name>(...
        after = line.split(":def ", 1)
        if len(after) != 2:
            after = line.split("def ", 1)
        frag = after[-1]
        name = frag.split("(", 1)[0].strip()
        if name:
            names.add(name)
    return names


def test_enumeration_completeness_every_step0_name_is_accounted_for() -> None:
    """AC2: every name the Step-0 command returns is EITHER a classified mutating member OR
    marked NON-MUTATING with a reason — no name may be silently absent."""
    names = _step0_names()
    accounted = _CLASSIFIED_MUTATING | set(_NON_MUTATING)
    unaccounted = names - accounted
    assert not unaccounted, (
        f"Step-0 enumeration returned name(s) neither classified as mutating nor marked "
        f"NON-MUTATING: {sorted(unaccounted)}. Every returned name must be accounted for — "
        f"an unclassified name is how a mutation silently inherits (or escapes) a 429 policy"
    )


def test_dc_crosscheck_every_dc_swept_mutation_is_covered_on_cloud() -> None:
    """AC2 cross-check: every member DC's sweep enumerates has a Cloud counterpart in this
    sweep's classification, so no DC-covered mutation is un-swept on Cloud."""
    missing = _DC_SWEPT - _CLASSIFIED_MUTATING
    assert not missing, (
        f"DC's rate-limit sweep covers member(s) Cloud's table omits: {sorted(missing)} — "
        f"either classify them or record why the divergence is legitimate"
    )


# ---------------------------------------------------------------------------
# ANTI-DRIFT GUARD (ticket 81d5, operator decision 2026-08-07 — option B).
#
# The sweep above stays scoped to the DECLARED transport surface
# (``_SWEPT_TRANSPORT_MODULES``); a package-wide glob was REJECTED because it would pull
# ~90 unrelated members across 8 modules into the 429 policy classification and silently
# redefine the sweep's subject from "the transport layer" to "the whole jira package",
# contradicting 8a5e's disposition.
#
# What the guard buys instead: drift detection over that declared surface. A classified
# mutating member cannot be renamed into, moved into, or newly defined in an UNSWEPT
# ``adapters/jira/*.py`` module and quietly escape the sweep — the only shape admitted
# outside the swept set is the deliberately narrow pure transport FACADE that
# ``backend.py`` already uses (one ``return self.transport.<same name>(...)`` forwarding the
# declared arguments unchanged). Such a facade issues no HTTP call and carries no 429
# policy of its own, so it is not a mutation site; anything else — a guard, a retry, a log
# line, an argument transform, an extra statement, or a non-``self.transport`` target — is.
#
# This is deliberately NARROWER than the rejected glob: it catches a classified NAME
# drifting out from under the sweep, not a brand-new mutating member under a brand-new
# name. That is exactly what option B was chosen to guarantee.
# ---------------------------------------------------------------------------


def _unswept_jira_modules() -> list[Path]:
    """Every ``adapters/jira`` Python module the sweep does NOT enumerate.

    DERIVED from the package listing minus ``_SWEPT_TRANSPORT_MODULES``, so a module added
    beside the swept four lands in the guard's scope the day it appears — including
    ``acli_subprocess.py`` and ``jira_fields.py``, which earlier hand-written scope notes
    omitted. ``__init__.py`` is excluded: it re-exports, it does not define members."""
    root = _repo_root()
    swept = {root / p for p in _SWEPT_TRANSPORT_MODULES}
    return sorted(
        p
        for p in (root / _JIRA_ADAPTER_PKG).glob("*.py")
        if p not in swept and p.name != "__init__.py"
    )


def _is_pure_transport_facade(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether ``fn`` is the ONE collision shape admitted outside the swept modules:

    ``def name(self, a, b, *, c): return self.transport.name(a, b, c=c)``

    Every clause below is load-bearing — each loosening reopens the hole the guard exists
    to close, so the predicate fails CLOSED on anything it does not positively recognise.
    An ``async def`` can never qualify (the transport surface is synchronous, and awaiting
    is itself behaviour the sweep would need to classify)."""
    if isinstance(fn, ast.AsyncFunctionDef):
        return False

    body = list(fn.body)
    # A docstring is not executable, so it does not make the facade impure. Nothing else
    # may precede the return.
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False
    call = body[0].value
    if not isinstance(call, ast.Call):
        return False

    # target must be exactly ``self.transport.<same name>``
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != fn.name:
        return False
    receiver = func.value
    if not isinstance(receiver, ast.Attribute) or receiver.attr != "transport":
        return False
    if not isinstance(receiver.value, ast.Name) or receiver.value.id != "self":
        return False

    # arguments must be FORWARDED UNCHANGED: no *args/**kwargs on either side, no
    # defaults re-supplied, no transforms, no re-ordering, no dropped or added arguments.
    spec = fn.args
    if spec.vararg or spec.kwarg or any(k.arg for k in call.keywords if k.arg is None):
        return False
    positional = [a.arg for a in (*spec.posonlyargs, *spec.args)]
    if not positional or positional[0] != "self":
        return False
    positional = positional[1:]
    if len(call.args) != len(positional):
        return False
    if any(
        not isinstance(arg, ast.Name) or arg.id != declared
        for arg, declared in zip(call.args, positional, strict=True)
    ):
        return False
    keyword_only = [a.arg for a in spec.kwonlyargs]
    if [k.arg for k in call.keywords] != keyword_only:
        return False
    return all(isinstance(k.value, ast.Name) and k.value.id == k.arg for k in call.keywords)


def _classified_collisions(source: str) -> list[tuple[str, bool]]:
    """``(name, is_pure_transport_facade)`` for every def in ``source`` whose name is in
    ``_CLASSIFIED_MUTATING`` — at ANY nesting depth, so hiding a mutation inside a nested
    class or a closure does not evade the guard."""
    return [
        (node.name, _is_pure_transport_facade(node))
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name in _CLASSIFIED_MUTATING
    ]


# The collisions that exist today and are known-benign (8a5e measured both as one-line pure
# delegations under backend.py's explicit "delegates to transport" sections).
_KNOWN_BENIGN_COLLISIONS = {
    ("backend.py", "set_relationship"),
    ("backend.py", "add_comment"),
}


def test_antidrift_scope_covers_every_unswept_jira_adapter_module() -> None:
    """AC1/AC2: the sweep itself stays scoped to the declared four transport modules, and
    the guard's scope is the DERIVED remainder of the package — explicitly including
    ``acli_subprocess.py`` and ``jira_fields.py``."""
    root = _repo_root()
    assert len(_SWEPT_TRANSPORT_MODULES) == 4, (
        f"the swept transport surface must stay the four modules 8a5e ruled correct, got "
        f"{list(_SWEPT_TRANSPORT_MODULES)} — widening it is a 429-policy scope decision, "
        f"not a refactor (ticket 81d5 rejected the package-wide glob)"
    )
    for rel in _SWEPT_TRANSPORT_MODULES:
        assert (root / rel).is_file(), f"declared swept transport module is missing: {rel}"

    unswept = {p.name for p in _unswept_jira_modules()}
    assert not unswept & {Path(p).name for p in _SWEPT_TRANSPORT_MODULES}, (
        f"the anti-drift scope must be DISJOINT from the swept set, got overlap in {unswept}"
    )
    for required in ("acli_subprocess.py", "jira_fields.py", "backend.py"):
        assert required in unswept, (
            f"{required} is an unswept adapters/jira module and must be inside the "
            f"anti-drift scope, got {sorted(unswept)}"
        )


def test_antidrift_unswept_classified_collisions_are_only_pure_transport_facades() -> None:
    """AC3/AC4/AC5: every def OUTSIDE the swept transport modules whose name is in
    ``_CLASSIFIED_MUTATING`` must be a pure same-name ``self.transport`` delegation — and
    the accepted ones are exactly the two known-benign backend facades."""
    accepted: set[tuple[str, str]] = set()
    offenders: list[str] = []
    for module in _unswept_jira_modules():
        for name, is_facade in _classified_collisions(module.read_text(encoding="utf-8")):
            if is_facade:
                accepted.add((module.name, name))
            else:
                offenders.append(f"{module.name}::{name}")

    assert not offenders, (
        f"unswept adapters/jira module(s) define classified MUTATING name(s) that are not "
        f"pure transport facades: {sorted(offenders)}. The Cloud 429 sweep only enumerates "
        f"{[Path(p).name for p in _SWEPT_TRANSPORT_MODULES]}, so such a definition carries "
        f"no verified 429 policy — either move it into a swept transport module, or reduce "
        f"it to a single `return self.transport.<same name>(...)` forwarding its arguments "
        f"unchanged"
    )
    assert accepted == _KNOWN_BENIGN_COLLISIONS, (
        f"the set of accepted transport facades outside the sweep changed: expected "
        f"{sorted(_KNOWN_BENIGN_COLLISIONS)}, got {sorted(accepted)}. A new one is a 429 "
        f"policy decision — record it here deliberately rather than letting it drift in"
    )


# name -> source of an UNSWEPT module defining a classified mutating name. Each REJECTED
# case is a shape that would silently escape the sweep; the ACCEPTED case pins that the
# guard is not vacuously red. Written against ``add_comment`` because it is the sharpest
# case: it is classified single-attempt BY DESIGN (a comment has no idempotency key), so a
# retry smuggled into an unswept copy is a real duplicate-write bug.
_FACADE_ACCEPTED = (
    "class B:\n    def add_comment(self, r, b):\n        return self.transport.add_comment(r, b)\n"
)
_FACADE_REJECTED: dict[str, str] = {
    "extra statement": (
        "class B:\n"
        "    def add_comment(self, r, b):\n"
        "        b = b.strip()\n"
        "        return self.transport.add_comment(r, b)\n"
    ),
    "guard": (
        "class B:\n"
        "    def add_comment(self, r, b):\n"
        "        if not b:\n"
        "            return None\n"
        "        return self.transport.add_comment(r, b)\n"
    ),
    "retry loop": (
        "class B:\n"
        "    def add_comment(self, r, b):\n"
        "        for _ in range(3):\n"
        "            return self.transport.add_comment(r, b)\n"
    ),
    "logging": (
        "class B:\n"
        "    def add_comment(self, r, b):\n"
        "        log.info('x')\n"
        "        return self.transport.add_comment(r, b)\n"
    ),
    "argument transform": (
        "class B:\n"
        "    def add_comment(self, r, b):\n"
        "        return self.transport.add_comment(r, b.upper())\n"
    ),
    "dropped argument": (
        "class B:\n    def add_comment(self, r, b):\n        return self.transport.add_comment(r)\n"
    ),
    "reordered arguments": (
        "class B:\n"
        "    def add_comment(self, r, b):\n"
        "        return self.transport.add_comment(b, r)\n"
    ),
    "renamed transport call": (
        "class B:\n"
        "    def add_comment(self, r, b):\n"
        "        return self.transport.append_comment(r, b)\n"
    ),
    "non-transport target": (
        "class B:\n    def add_comment(self, r, b):\n        return self.client.add_comment(r, b)\n"
    ),
    "self-call": (
        "class B:\n    def add_comment(self, r, b):\n        return self._add_comment(r, b)\n"
    ),
    "module-level function": ("def add_comment(r, b):\n    return transport.add_comment(r, b)\n"),
    "star-args passthrough": (
        "class B:\n"
        "    def add_comment(self, *args, **kw):\n"
        "        return self.transport.add_comment(*args, **kw)\n"
    ),
    "async facade": (
        "class B:\n"
        "    async def add_comment(self, r, b):\n"
        "        return self.transport.add_comment(r, b)\n"
    ),
    "no return": (
        "class B:\n    def add_comment(self, r, b):\n        self.transport.add_comment(r, b)\n"
    ),
    "nested-class mutation": (
        "class Outer:\n"
        "    class Inner:\n"
        "        def add_comment(self, r, b):\n"
        "            return urlopen(r, b)\n"
    ),
}


def test_antidrift_guard_accepts_the_exact_facade_shape() -> None:
    """AC3: the deliberately narrow facade shape backend.py uses is ACCEPTED, so the guard
    is not vacuously red (a guard nothing can pass would be deleted, not obeyed)."""
    assert _classified_collisions(_FACADE_ACCEPTED) == [("add_comment", True)]


@pytest.mark.parametrize("shape", sorted(_FACADE_REJECTED))
def test_antidrift_guard_rejects_unswept_non_facade_mutation(shape: str) -> None:
    """AC5: a moved/new unswept definition using a classified mutating name turns the guard
    RED unless it is EXACTLY the pure-facade shape."""
    collisions = _classified_collisions(_FACADE_REJECTED[shape])
    assert collisions, f"fixture {shape!r} defines no classified mutating name — bad fixture"
    assert all(not is_facade for _, is_facade in collisions), (
        f"the anti-drift guard ACCEPTED a non-facade unswept definition ({shape}) — that is "
        f"the exact drift this guard exists to catch: a classified mutating name living "
        f"outside the swept transport modules while carrying behaviour of its own"
    )
