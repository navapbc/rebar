"""Test harness for the serial auto-lander (epic f1fa). Puts `infra/` on sys.path so the
standalone `autolander` package (which is NOT part of `src/rebar`) imports as `autolander.*`,
and provides two fakes: a `RecordingClient` (duck-typed GerritClient for loop-logic tests)
and a `FakeTransport` (HTTP-boundary seam for the real GerritClient)."""

from __future__ import annotations

import sys
from pathlib import Path

_INFRA = Path(__file__).resolve().parents[2] / "infra"
if str(_INFRA) not in sys.path:
    sys.path.insert(0, str(_INFRA))


def change_info(
    change_id: str,
    number: int,
    *,
    autosubmit_date: str | None = None,
    submittable: bool = True,
    parents: int = 1,
    verified: bool = False,
    status: str = "NEW",
    revision: str | None = None,
    owner_account: int = 1000,
) -> dict:
    """Build a minimal ChangeInfo (o=DETAILED_LABELS + CURRENT_REVISION/COMMIT shape).

    `verified=True` adds a fresh `Verified +1` on the current patchset; `status` is the
    change status (e.g. "MERGED" after submit); `revision` overrides the current SHA.
    """
    labels: dict = {}
    if autosubmit_date is not None:
        labels["Autosubmit"] = {"all": [{"value": 1, "date": autosubmit_date, "_account_id": 1000}]}
    if verified:
        labels["Verified"] = {
            "approved": {"_account_id": 2000},
            "all": [{"value": 1, "_account_id": 2000}],
        }
    else:
        labels["Verified"] = {"all": [{"value": 0, "_account_id": 2000}]}
    rev = revision or ("rev" + str(number))
    return {
        "change_id": change_id,
        "_number": number,
        "status": status,
        "submittable": submittable,
        "labels": labels,
        "owner": {"_account_id": owner_account},
        "current_revision": rev,
        "revisions": {rev: {"commit": {"parents": [{"commit": f"p{i}"} for i in range(parents)]}}},
    }


class RecordingClient:
    """Duck-typed stand-in for GerritClient that records mutating calls and serves canned
    reads. Lets loop-logic tests assert selection + routing WITHOUT any HTTP."""

    def __init__(
        self,
        *,
        query_result: list[dict] | None = None,
        related: dict[str, list[dict]] | None = None,
        changes: dict[str, dict] | None = None,
        change_seq: dict[str, list[dict]] | None = None,
        submit_error: Exception | None = None,
        rebase_error: Exception | None = None,
        set_review_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.set_review_errors = set_review_errors or {}
        self.rebase_error = rebase_error
        self.query_result = query_result or []
        self.related = related or {}
        self.changes = changes or {c["change_id"]: c for c in self.query_result}
        # Per-change QUEUE of get_change responses (for TOCTOU re-drive: e.g. not-landable
        # then landable). Each get_change pops the next; the last repeats.
        self.change_seq = {k: list(v) for k, v in (change_seq or {}).items()}
        self.submit_error = submit_error
        self.calls: list[tuple] = []

    def query_changes(self, query, opts=None):
        self.calls.append(("query", query, {"opts": opts}))
        return list(self.query_result)

    def get_change(self, change_id, opts=None):
        self.calls.append(("get_change", change_id, {"opts": opts}))
        seq = self.change_seq.get(change_id)
        base = (
            seq.pop(0) if (seq and len(seq) > 1) else (seq[0] if seq else self.changes[change_id])
        )
        # Mirror Gerrit: `submittable` is only returned when the SUBMITTABLE option is asked
        # for. A caller that forgets o=SUBMITTABLE gets no `submittable` key (-> reads as
        # None), which is exactly the bug the live E2E surfaced.
        if "SUBMITTABLE" not in (opts or []):
            base = {k: v for k, v in base.items() if k != "submittable"}
        return base

    def set_review(self, change_id, *, message=None, labels=None):
        self.calls.append(("set_review", change_id, {"message": message, "labels": labels}))
        if change_id in self.set_review_errors:
            raise self.set_review_errors[change_id]
        return {"labels": labels or {}}

    def get_related(self, change_id):
        return list(self.related.get(change_id, []))

    def rebase(self, change_id, *, on_behalf_of_uploader=True):
        self.calls.append(("rebase", change_id, {"on_behalf_of_uploader": on_behalf_of_uploader}))
        if self.rebase_error is not None:
            raise self.rebase_error
        return {"change_id": change_id}

    def rebase_chain(self, change_id, *, on_behalf_of_uploader=True):
        self.calls.append(
            ("rebase:chain", change_id, {"on_behalf_of_uploader": on_behalf_of_uploader})
        )
        return {"change_id": change_id}

    def submit(self, change_id):
        self.calls.append(("submit", change_id, {}))
        if self.submit_error is not None:
            err, self.submit_error = self.submit_error, None  # raise once, then succeed
            raise err
        return {"change_id": change_id, "status": "MERGED"}

    def mutating_calls(self):
        return [c for c in self.calls if c[0] in ("rebase", "rebase:chain", "submit")]


class FakeTransport:
    """Records (method, path, body) and returns canned `(status, text)` per route. `text`
    is returned verbatim (tests deliberately include Gerrit's XSSI prefix to exercise strip)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self._routes: dict[tuple[str, str], tuple[int, str]] = {}

    def route(self, method: str, path_contains: str, status: int, text: str) -> FakeTransport:
        self._routes[(method, path_contains)] = (status, text)
        return self

    def __call__(self, method: str, path: str, body: dict | None):
        self.calls.append((method, path, body))
        for (m, frag), resp in self._routes.items():
            if m == method and frag in path:
                return resp
        return (404, ")]}'\n{}")
