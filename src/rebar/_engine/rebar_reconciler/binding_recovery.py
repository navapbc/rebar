"""Incomplete-operation recovery for binding state (RP-02 S3).

``BindingRecovery`` owns the repair half of binding lifecycle: what to do when a pass
died PART WAY THROUGH a multi-write operation. Two such operations exist, and they leave
very different residue:

* **An interrupted CREATE** leaves a ``pending`` binding. The write-ahead protocol
  (story 9622) makes that state deterministic to resolve — a keyed-pending entry proves
  the create landed, a keyless one does not — and ``recover_pending_bindings`` is that
  resolution, lifted here UNCHANGED from the facade so the policy sits beside the other
  recovery it is a sibling of.
* **An interrupted RETIREMENT** leaves one identity both live and tombstoned.
  ``BindingLifecycle.retire`` writes the tombstone FIRST and drops the live
  forward/reverse pair second (ADR 0099 §5), so the only crash window it can leave is
  that ordered overlap — recoverable precisely because the identity matches on both
  sides. ``complete_interrupted_retirements`` finishes those, and REFUSES to touch
  anything that is not an exact match.

Like every owner behind the facade, this is not a store and not a persistence owner:
:class:`BindingRepository` owns every byte on disk and ``BindingLifecycle`` owns the
identity transitions. This class mutates the repository's OWN dictionaries in place and
reaches transitions through the lifecycle owner, so a repair is indistinguishable from
the ordinary operation it completes.

The asymmetry that governs the whole retirement half: **a tombstone is authoritative
retirement INTENT, and only ``unretire`` revokes it.** So completion deletes live
residue and NEVER a tombstone. Getting that backwards would turn a soft delete into a
hard one — exactly the incident the retired-first write order exists to prevent.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rebar_reconciler.timeutil import utc_now_iso

if TYPE_CHECKING:
    from ._backend import TicketTransport
    from .binding_lifecycle import BindingLifecycle
    from .binding_repository import BindingRepository

__all__ = [
    "BindingRecovery",
    "RetirementAbort",
    "RetirementRepair",
    "RetirementRepairOutcome",
    "classify_interrupted_retirements",
]

#: How long a keyless-pending binding is treated as "the create may have landed but is not
#: indexed yet" (bug 21fc). Jira DC's Lucene index is eventually consistent and
#: JRASERVER-70423 documents a 2,991s lag, so this is deliberately LARGER: the cost of
#: waiting is a delayed create, of not waiting a duplicate — only one is reversible.
_INDEX_LAG_GRACE_SECONDS = 3600.0

#: Consecutive negative searches required before absence is treated as corroborated (a
#: single miss is exactly what a lagging index produces for an issue that DOES exist).
_MISSES_BEFORE_UNBIND = 3

#: Why a tombstone's live residue was left alone. Each value names the pair of identities
#: that disagreed, because the operator's next question is always "disagreed HOW?" — a
#: generic "unsafe" would send them back to the raw files to find out.
ABORT_FORWARD_MISSING = "forward_missing"
ABORT_FORWARD_KEY_MISMATCH = "forward_key_mismatch"
ABORT_REVERSE_MISSING = "reverse_missing"
ABORT_REVERSE_MISMATCH = "reverse_mismatch"
ABORT_TOMBSTONE_LOCAL_MISSING = "tombstone_local_missing"
ABORT_MALFORMED_ENTRY = "malformed_entry"
ABORT_REPLACE_FAILED = "replace_failed"


def _now_iso() -> str:
    # Canonical Z-suffix UTC (twin of rebar.timeutils.utc_now_iso); the local spelling
    # used across this module's call sites, matching ``binding_lifecycle``'s.
    return utc_now_iso()


def _age_seconds(created_at: Any) -> float:
    """Seconds since an ISO-8601 ``created_at``; ``inf`` when it is absent or unparseable.

    ``inf`` is the SAFE default here and the direction matters: an entry whose age cannot
    be established is treated as OLD, so it becomes eligible for the ordinary
    corroborated-unbind path rather than being suppressed forever. A store written before
    this field existed must not strand its tickets.
    """
    if not isinstance(created_at, str) or not created_at:
        return float("inf")
    from datetime import datetime, timezone

    try:
        stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


@dataclass(frozen=True)
class RetirementRepair:
    """One tombstone whose live residue matched exactly and was removed."""

    jira_key: str
    local_id: str


@dataclass(frozen=True)
class RetirementAbort:
    """One tombstone that could not be completed safely, with its evidence.

    ``evidence`` carries the identities that disagreed, JSON-serialisable, so an operator
    reading a report can see WHICH sides conflicted without re-deriving the state from
    the two files — by the time anyone looks, the store has usually moved on.
    """

    jira_key: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetirementRepairOutcome:
    """What one completion attempt did and refused to do.

    Both halves are reported. A repair that silently dropped its refusals would make an
    inconsistent store look healthy, which is the failure mode that lets a partial
    retirement survive pass after pass unnoticed.
    """

    completed: tuple[RetirementRepair, ...] = ()
    aborted: tuple[RetirementAbort, ...] = ()


def _usable_local_id(tombstone: Any) -> str | None:
    """The tombstone's ``local_id``, or ``None`` when it cannot identify a live entry.

    A tombstone is read back from a file shared with other writers, so neither its shape
    nor its fields can be assumed: a legacy list-form row, a hand-edited ``null``, or an
    empty string all arrive here. None of them can address a forward entry, so they are
    collapsed to ``None`` and handled by one abort reason rather than raising.
    """
    if not isinstance(tombstone, dict):
        return None
    local_id = tombstone.get("local_id")
    if not isinstance(local_id, str) or not local_id:
        return None
    return local_id


def _evidence(
    tombstone_local_id: Any,
    entry: Any,
    reverse: dict[str, Any],
    jira_key: str,
) -> dict[str, Any]:
    """Assemble the three identities a human needs to adjudicate a refusal.

    Only the sides that EXIST are recorded. An absent key is itself the finding — an
    evidence dict padded with ``None`` reads as "we looked and found nothing there",
    which is a different claim from "there was nothing to look at".
    """
    evidence: dict[str, Any] = {"tombstone_local_id": tombstone_local_id}
    if isinstance(entry, dict):
        evidence["forward_jira_key"] = entry.get("jira_key")
    if jira_key in reverse:
        evidence["reverse_local_id"] = reverse[jira_key]
    return evidence


def _classify_one(
    jira_key: str,
    tombstone: Any,
    bindings: dict[str, Any],
    reverse: dict[str, Any],
) -> RetirementRepair | RetirementAbort | None:
    """Classify ONE tombstone: completable, refused, or nothing to say.

    ``None`` — silence — is the overwhelmingly common answer and the reason this returns
    three things rather than two. In a healthy store every tombstone has NO live residue,
    because retirement finished; reporting that as an abort would bury the one genuine
    finding under a report the size of the retired file.

    The check order is a contract. The forward entry's own ``jira_key`` is compared
    BEFORE either reverse-index check, because a forward entry pointing at a DIFFERENT
    key is not a half-finished retirement of this key at all — it is a live binding that
    happens to share a local id, and blaming the reverse index for it would name the
    wrong conflict and invite the wrong repair.
    """
    local_id = _usable_local_id(tombstone)
    forward_present = local_id is not None and local_id in bindings
    reverse_present = jira_key in reverse
    if not forward_present and not reverse_present:
        return None

    raw_local_id = tombstone.get("local_id") if isinstance(tombstone, dict) else None

    def abort(reason: str, entry: Any = None) -> RetirementAbort:
        return RetirementAbort(
            jira_key=jira_key,
            reason=reason,
            evidence=_evidence(raw_local_id, entry, reverse, jira_key),
        )

    if local_id is None:
        return abort(ABORT_TOMBSTONE_LOCAL_MISSING)
    if not forward_present:
        return abort(ABORT_FORWARD_MISSING)
    entry = bindings[local_id]
    if not isinstance(entry, dict):
        return abort(ABORT_MALFORMED_ENTRY, entry)
    if entry.get("jira_key") != jira_key:
        return abort(ABORT_FORWARD_KEY_MISMATCH, entry)
    if not reverse_present:
        return abort(ABORT_REVERSE_MISSING, entry)
    if reverse[jira_key] != local_id:
        return abort(ABORT_REVERSE_MISMATCH, entry)
    return RetirementRepair(jira_key=jira_key, local_id=local_id)


def classify_interrupted_retirements(
    bindings: dict[str, Any],
    reverse: dict[str, Any],
    retired_entries: dict[str, Any],
) -> RetirementRepairOutcome:
    """PURE classification of interrupted-retirement states. Never mutates its inputs.

    Kept pure and separate from the completion that acts on it so the DECISION can be
    exercised — and reasoned about by an operator — without a repository, a temp directory
    or a write. The three arguments are the repository's own live dictionaries at the call
    site, so mutating them here would perform a repair nobody asked for.

    Tombstones are visited in sorted key order. The report is read by humans and diffed
    between passes, so a dict-insertion-order walk would make an unchanged store produce
    a reshuffled answer.
    """
    completed: list[RetirementRepair] = []
    aborted: list[RetirementAbort] = []
    for jira_key in sorted(retired_entries):
        verdict = _classify_one(jira_key, retired_entries[jira_key], bindings, reverse)
        if isinstance(verdict, RetirementRepair):
            completed.append(verdict)
        elif isinstance(verdict, RetirementAbort):
            aborted.append(verdict)
    return RetirementRepairOutcome(completed=tuple(completed), aborted=tuple(aborted))


def _report_retirement_repair(outcome: RetirementRepairOutcome) -> None:
    """Announce a completion or a refusal on stderr; say NOTHING when neither happened.

    A repair mutates durable binding state that nobody asked for, in the middle of a pass
    whose operator asked for a sync. A silent one is indistinguishable from a store that
    was coherent all along, so the change has to be attributable to the pass that made it.

    Silence on the empty outcome is the load-bearing half. Every write-bearing pass reaches
    this, and in a healthy store every pass finds nothing; a per-pass "nothing to repair"
    line would be the overwhelming majority of what this ever prints and would train
    operators to skip the one line that matters. So the healthy pass says nothing at all.

    Refusals get their own line, and it is the more important of the two: a completion is
    the system repairing itself, while a refusal is an inconsistent store that will NOT fix
    itself and needs a human to adjudicate. Each refusal carries its reason, because
    "refused" without the disagreement names no next step.

    Why stderr and not the pass's structured ``sync_logger``: this owner is deliberately
    given no logger. Every other reconciler observation of this kind — the rich-reemit
    lines in ``apply_handlers``, the recovery-failure line in ``run_differs`` — uses the
    same ``RECON:`` stderr convention, so an operator reads them together. Threading the
    logger down would also have to widen the single call line in ``reconcile.py``, which
    sits at 799 of the 800-line cap and cannot afford the wrap. Promoting these to
    structured pass events is worth doing when that file is next split; it is recorded as
    a residual gap rather than smuggled in here.
    """
    if outcome.completed:
        keys = ",".join(repair.jira_key for repair in outcome.completed)
        print(f"RECON: retirement_repair_completed keys={keys}", file=sys.stderr)
    if outcome.aborted:
        refused = ",".join(f"{abort.jira_key}:{abort.reason}" for abort in outcome.aborted)
        print(f"RECON: retirement_repair_refused keys={refused}", file=sys.stderr)


class BindingRecovery:
    """Owns incomplete-operation recovery over the repository's own dictionaries."""

    def __init__(self, repository: BindingRepository, lifecycle: BindingLifecycle) -> None:
        """Attach to the SAME repository and lifecycle the facade coordinates.

        Both owners are held by reference and nothing is snapshotted: a repair reaches
        live state through ``repository`` on every access and performs its transitions
        through ``lifecycle``, so it cannot invent a second route into binding state. That
        is the whole point of taking the lifecycle owner as an argument rather than
        re-implementing confirm/unbind here — a recovered binding must be byte-identical
        to one the ordinary path produced, or the store slowly acquires two dialects.
        """
        self._repo = repository
        self._lifecycle = lifecycle

    def recover_pending_bindings(
        self, client: TicketTransport, *, failure_sink: list[dict[str, Any]] | None = None
    ) -> int:
        """Scan for pending bindings and attempt to recover (story 9622).

        For each pending binding:

        - **Keyed-pending** (entry carries a ``jira_key`` — the write-ahead
          recorded it the instant ``create_issue`` returned, before the label was
          attached): the create landed, so retro-attach the rebar-id label +
          ``local_id`` entity property (idempotent) and confirm. NO Jira search —
          deterministic, so a crash in the create->label window yields NO duplicate.
        - **Keyless-pending** (no ``jira_key`` — crash before/during create): search
          Jira for the ``rebar-id:{local_id}`` label (colon form, legacy hyphen
          fallback). Confirm if found; unbind if not (the create never reached Jira).

        Any per-entry error is appended to ``failure_sink`` as ``{local_id, reason}``
        and skipped (loud but non-fatal; the entry stays pending). Returns the count
        of RESOLVED bindings (confirmed or unbound). ``client`` must expose
        ``search_issues`` / ``add_label`` / ``set_entity_property``.

        Takes no persistence or scope parameter, and gains none by living here: whether
        recovery runs at all is the CALLER's decision (``run_differs`` skips it for a
        scoped pass), and durability comes from the pass's later unconditional ``save()``.
        """
        bindings = self._repo.bindings
        pending = [lid for lid, entry in bindings.items() if entry.get("state") == "pending"]
        recovered = 0
        for local_id in list(pending):
            try:
                entry = bindings.get(local_id) or {}
                keyed = entry.get("jira_key")
                if keyed:
                    # Deterministic: the key is known — retro-attach the identity
                    # marker (idempotent) so future JQL dedup can find the issue,
                    # then confirm. No search.
                    client.add_label(keyed, f"rebar-id:{local_id}")
                    client.set_entity_property(keyed, "local_id", local_id)
                    self._lifecycle.bind_confirm(local_id, keyed)
                    recovered += 1
                    continue
                # Keyless: canonical colon-form label (applier.py outbound/inbound).
                colon_label = f"rebar-id:{local_id}"
                results = client.search_issues(f'labels = "{colon_label}"')
                if not results:
                    # Legacy fallback: hyphen-form (pre-colon-migration issues).
                    hyphen_label = f"rebar-id-{local_id}"
                    results = client.search_issues(f'labels = "{hyphen_label}"')
                if results:
                    self._lifecycle.bind_confirm(local_id, results[0]["key"])
                    recovered += 1
                    continue
                # A NEGATIVE SEARCH IS NOT PROOF OF ABSENCE ON DC (bug 21fc): the keyless
                # state is entered on a crash during create_issue — exactly when the issue
                # may exist but not yet be indexed (Jira DC's Lucene index is eventually
                # consistent, JRASERVER-70423: 2,991s lag). Unbinding here makes the next
                # pass create a duplicate, so absence must be CORROBORATED: repeated misses
                # AND an entry too old for the documented lag to explain. Else stay pending.
                misses = int(entry.get("search_miss_count") or 0) + 1
                entry["search_miss_count"] = misses
                entry["updated_at"] = _now_iso()
                if (
                    misses >= _MISSES_BEFORE_UNBIND
                    and _age_seconds(entry.get("created_at")) >= _INDEX_LAG_GRACE_SECONDS
                ):
                    self._lifecycle.unbind(local_id)
                    recovered += 1
            except Exception as exc:  # noqa: BLE001 — loud-but-non-fatal: record and continue
                if failure_sink is not None:
                    failure_sink.append({"local_id": local_id, "reason": repr(exc)})
        return recovered

    def repair_at_write_boundary(self, *, persist: bool, scoped: bool) -> RetirementRepairOutcome:
        """The ONLY door a reconcile pass may use to complete interrupted retirements.

        Two conditions have to hold, and the guard lives HERE rather than at the call site
        so the spine carries one unconditional line and cannot drift from the policy:

        * ``persist`` — a cap-0 mode (dry-run, reconcile-check) is documented read-only, and
          completing a retirement is a write. The overlap simply survives to the next
          write-bearing pass, which is the correct outcome: a read-only command that
          silently mutated the store would be the more surprising bug.
        * ``not scoped`` — a filtered pass reasons about a hand-picked subset of tickets, so
          finishing retirements for identities outside that scope is a scope leak. The
          repair is store-wide by nature (it walks every tombstone) and cannot be narrowed
          honestly, so a scoped pass declines the whole operation rather than half of it.

        Refusing is FREE: no classification runs, nothing is read from the repository, and
        nothing is reported. That matters because this is reached on every read-only pass,
        and a guard that classified first and discarded the answer would make the read-only
        path pay for a repair it is forbidden to perform.

        Returns an EMPTY :class:`RetirementRepairOutcome` when refused — the same type the
        admitted path returns, so a caller never has to branch on "was I allowed?".
        """
        if not persist or scoped:
            return RetirementRepairOutcome()
        outcome = self.complete_interrupted_retirements()
        _report_retirement_repair(outcome)
        return outcome

    def complete_interrupted_retirements(self) -> RetirementRepairOutcome:
        """Finish retirements the retired-first order left half-done (ADR 0099 §5).

        The tombstone is durable; only the live forward/reverse pair is still there. So
        the repair is to delete that residue and leave the tombstone completely alone —
        consuming it would make the soft delete unrecoverable, which is the one outcome
        the write order was chosen to rule out. ``save_retired`` is therefore never
        called from here at all; ``unretire`` remains the only route back.

        NOTHING is written when there is no exact-match candidate, even when there ARE
        refusals. Every pass would otherwise rewrite (and re-commit to the tickets
        branch) the whole live store just to report that it changed nothing, and a store
        holding one permanently-refused tombstone would churn forever.

        One ``save()`` covers the whole batch: the removals are independent, and a
        per-candidate save would leave a crash mid-batch in the same partial state this
        method exists to clean up.

        A failed save is ROLLED BACK in memory. The live file still holds every pair, so
        leaving them popped would hand the rest of the pass a view that disagrees with
        disk — and the next ``save()`` from any other owner would then commit a deletion
        this method already declined to report as done. The failure is returned as
        ``ABORT_REPLACE_FAILED`` per attempted candidate rather than raised, because a
        binding repair must never be the thing that aborts a sync pass.
        """
        repo = self._repo
        outcome = classify_interrupted_retirements(
            repo.bindings, repo.reverse, repo.retired_entries()
        )
        if not outcome.completed:
            return outcome
        removed: list[tuple[RetirementRepair, Any, Any]] = []
        for repair in outcome.completed:
            entry = repo.bindings.pop(repair.local_id, None)
            reverse_local = repo.reverse.pop(repair.jira_key, None)
            removed.append((repair, entry, reverse_local))
        try:
            repo.save()
        except Exception as exc:  # noqa: BLE001 — a repair must not abort the pass
            return RetirementRepairOutcome(
                completed=(),
                aborted=outcome.aborted + tuple(self._roll_back(removed, exc)),
            )
        return outcome

    def _roll_back(
        self,
        removed: list[tuple[RetirementRepair, Any, Any]],
        exc: BaseException,
    ) -> list[RetirementAbort]:
        """Put every popped pair back and report each attempt as a refusal.

        Restoring is what keeps the in-memory view equal to the bytes on disk after a
        failed replacement; the aborts are what keep the failure visible, since an
        outcome reporting neither a completion nor a refusal is indistinguishable from a
        store that had nothing to repair.
        """
        aborts: list[RetirementAbort] = []
        for repair, entry, reverse_local in removed:
            if entry is not None:
                self._repo.bindings[repair.local_id] = entry
            if reverse_local is not None:
                self._repo.reverse[repair.jira_key] = reverse_local
            aborts.append(
                RetirementAbort(
                    jira_key=repair.jira_key,
                    reason=ABORT_REPLACE_FAILED,
                    evidence={
                        "tombstone_local_id": repair.local_id,
                        "forward_jira_key": repair.jira_key,
                        "reverse_local_id": repair.local_id,
                        "error": repr(exc),
                    },
                )
            )
        return aborts
