"""Serial auto-lander core loop (epic f1fa / S2 — S2a portion: selection + rebase-routing).

S2a implements the loop's FIRST step: pick the front `Autosubmit`+submittable change/chain
(FIFO by the `Autosubmit` vote's approval date) and route it to the correct Gerrit rebase
call. The wipChain state machine (S2b), fresh-Verified-await + ancestor-atomic submit (S2c),
and failure handling (S3) build on this.

STDLIB-ONLY, no `import rebar`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from autolander.gerrit import GerritClient

# Selection query: open, has an Autosubmit +1, and Gerrit already considers it submittable
# (both gate votes at MAX AND on-tip under Fast-Forward-Only main, ADR-0040).
SELECTION_QUERY = "status:open label:Autosubmit+1 is:submittable"

# Change classification (routes the rebase call).
KIND_SINGLE = "single"  # a lone non-merge change -> POST /rebase
KIND_CHAIN = "chain"  # a >1-member linear non-merge relation chain -> POST /rebase:chain
KIND_MERGE = (
    "merge"  # a --no-ff merge change -> POST /rebase (first-parent only), NEVER rebase:chain
)


@dataclass
class Candidate:
    """The selected front change/chain to land."""

    change_id: str
    number: int
    autosubmit_date: str  # the Autosubmit +1 ApprovalInfo.date (FIFO key)
    kind: str  # one of KIND_SINGLE / KIND_CHAIN / KIND_MERGE
    member_ids: list[str] = field(
        default_factory=list
    )  # chain members bottom->top; [change_id] otherwise


def autosubmit_approval_date(change: dict) -> str | None:
    """Return the `date` of the `Autosubmit` label's +1 ApprovalInfo on `change`
    (from an `o=DETAILED_LABELS` query), or None when there is no such vote."""
    label = (change.get("labels") or {}).get("Autosubmit") or {}
    for approval in label.get("all") or []:
        if approval.get("value") == 1:
            return approval.get("date")
    return None


def classify_change(client: GerritClient, change: dict) -> tuple[str, list[str]]:
    """Classify `change` for rebase-routing.

    Returns `(kind, member_ids)`:
      - KIND_MERGE  when the current revision commit has >1 parent (a --no-ff merge).
      - KIND_CHAIN  when RelatedChanges reports a >1-member linear (non-merge) relation chain.
      - KIND_SINGLE otherwise.
    `member_ids` are the chain members (bottom-most first) for a chain, else `[change_id]`.
    """
    change_id = change.get("change_id")

    # Merge detection takes precedence: >1 parent on the current revision commit.
    parents = None
    current = change.get("current_revision")
    revisions = change.get("revisions") or {}
    rev = revisions.get(current) if current else None
    if isinstance(rev, dict):
        parents = (rev.get("commit") or {}).get("parents")
    if parents is None:
        fetched = client.get_change(change_id, ["CURRENT_REVISION", "CURRENT_COMMIT"])
        cur = fetched.get("current_revision")
        frev = (fetched.get("revisions") or {}).get(cur) or {}
        parents = (frev.get("commit") or {}).get("parents") or []
    if len(parents) > 1:
        return KIND_MERGE, [change_id]

    # Chain: RelatedChanges with >1 member, in the order given.
    related = client.get_related(change_id)
    if len(related) > 1:
        member_ids = [m.get("change_id") or m.get("_change_number") for m in related]
        return KIND_CHAIN, member_ids

    return KIND_SINGLE, [change_id]


def select_front_candidate(client: GerritClient) -> Candidate | None:
    """Select the front change/chain to land: the OLDEST-voted submittable `Autosubmit`
    change (FIFO on the Autosubmit vote's `ApprovalInfo.date`). Returns None when the pool
    is empty."""
    changes = client.query_changes(SELECTION_QUERY, ["DETAILED_LABELS"])
    if not changes:
        return None

    dated = [(autosubmit_approval_date(c), c) for c in changes]
    dated = [(d, c) for (d, c) in dated if d is not None]
    if not dated:
        return None

    date, change = min(dated, key=lambda pair: pair[0])
    kind, member_ids = classify_change(client, change)
    return Candidate(
        change_id=change.get("change_id"),
        number=change.get("_number"),
        autosubmit_date=date,
        kind=kind,
        member_ids=member_ids,
    )


def route_rebase(client: GerritClient, candidate: Candidate) -> str:
    """Route `candidate` to the correct Gerrit rebase call, preserving the uploader
    (`rebase_on_behalf_of_uploader=true`, so author/DCO/`rebar-ticket` trailers survive and
    the rebase drops `Verified` -> CI re-runs). Returns the endpoint kind actually invoked:
    `"rebase:chain"` for a KIND_CHAIN candidate, `"rebase"` for KIND_SINGLE / KIND_MERGE."""
    if candidate.kind == KIND_CHAIN:
        client.rebase_chain(candidate.change_id, on_behalf_of_uploader=True)
        return "rebase:chain"
    client.rebase(candidate.change_id, on_behalf_of_uploader=True)
    return "rebase"
