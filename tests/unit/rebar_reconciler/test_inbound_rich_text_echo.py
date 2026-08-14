"""Inbound echo-safety for the lossy rich-text wire (story 3289, epic 708d).

This is the INBOUND mirror of ``test_rich_text_echo_suppression.py`` (which only covered
the OUTBOUND differ). The gap it guards was live-observed: under the DC rich-text cutover
the local body is Markdown while Jira echoes the RENDERED wiki, so the inbound differ,
which normalized descriptions through the Cloud ADF codec only, saw Jira's echo of our OWN
push as a fresh Jira-side change and emitted an inbound description update on every pass —
pulling the wiki wire form back over the user's local Markdown and breaking the AC2
once-only-upgrade-then-converge guarantee.

The fix injects the outbound port into the inbound differ and treats Jira's value as
unchanged when it equals the local body's LANDED wire form
(``map_fields_to_remote(local)``) — the inbound analogue of the outbound
``_baseline_form_matches``. A deterministic lossy fake codec is used here (no pandoc
dependency) so the convergence contract is exercised on every CI run, not only where the
real renderer is installed.
"""

from __future__ import annotations

from typing import Any

from rebar_reconciler.inbound_differ import _diff_jira_vs_local, compute_inbound_mutations

_MD = "# Heading\n\nProse with **bold**.\n\n{code}\nx = 1\n{code}\n"


def _to_wiki(markdown: str) -> str:
    """A deterministic, lossy Markdown->wiki transform standing in for the DC codec.

    Lossy in the same way the real renderer is: the wiki form does NOT round-trip back to
    the Markdown source, so a raw compare can never conclude "unchanged". Enough to drive
    the differ's landed-form path without requiring pandoc.
    """
    return markdown.replace("# ", "h1. ").replace("**", "*")


class _LossyOutboundMapper:
    """Minimal ``OutboundMapper`` whose description op is the lossy wiki render."""

    def map_fields_to_remote(
        self,
        changed: dict[str, Any],
        ticket: dict[str, Any] | None = None,
        binding_store: Any | None = None,
        local_ticket_types: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        out = dict(changed)
        value = out.get("description")
        if isinstance(value, str):
            out["description"] = _to_wiki(value)
        return out

    def map_local_to_remote(self, *a: Any, **k: Any) -> dict[str, Any]:  # pragma: no cover
        return {}

    def resolve_assignee(self, *a: Any, **k: Any) -> tuple[Any, bool, bool]:  # pragma: no cover
        return (None, False, False)


class _IdentityInboundMapper:
    """Minimal ``InboundMapper`` — remote fields are already in local shape here."""

    def map_remote_to_local(self, remote_fields: dict[str, Any]) -> dict[str, Any]:
        return dict(remote_fields)

    def normalize_rich_text(self, body: Any) -> str:  # pragma: no cover - unused here
        return "" if body is None else str(body)


class _BindingStore:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def get_local_id(self, jira_key: str) -> str | None:
        return self._mapping.get(jira_key)


def _local(desc: str) -> dict[str, Any]:
    return {
        "ticket_id": "T-1",
        "title": "t",
        "description": desc,
        "priority": 2,
        "status": "open",
    }


def _jira(desc: str) -> dict[str, Any]:
    return {"title": "t", "description": desc, "priority": 2, "status": "open"}


def _diff(local_desc: str, jira_desc: str) -> dict[str, Any]:
    return _diff_jira_vs_local(
        _jira(jira_desc),
        _local(local_desc),
        binding_store=None,
        inbound_mapper=_IdentityInboundMapper(),
        outbound_mapper=_LossyOutboundMapper(),
    )


def test_inbound_ignores_jiras_wiki_echo_of_our_own_push() -> None:
    """The crux: Jira echoes the RENDERED wiki of a body we pushed; the inbound differ
    must recognize it as the landed form of the local Markdown and emit NOTHING."""
    echo = _to_wiki(_MD)
    assert echo != _MD  # the codec is genuinely lossy — a raw compare would re-emit forever

    changed = _diff(_MD, echo)

    assert "description" not in changed


def test_inbound_still_emits_a_genuine_jira_side_edit() -> None:
    """Suppression must not swallow a real Jira-side change: a body that is NOT our
    landed echo still flows inbound."""
    edited = _to_wiki(_MD) + "\nh2. An operator edit on the Jira side\n"

    changed = _diff(_MD, edited)

    assert "description" in changed


def test_inbound_echo_converges_across_repeat_passes() -> None:
    """The 2nd-pass AC2 assertion, offline: repeated passes over the echo stay quiet."""
    echo = _to_wiki(_MD)
    for _ in range(5):
        assert "description" not in _diff(_MD, echo)


def test_orchestrator_threads_outbound_mapper_into_the_inbound_diff() -> None:
    """End-to-end through ``compute_inbound_mutations``: the echo must not surface as an
    inbound mutation, proving the orchestrator wires ``outbound_mapper`` all the way down."""
    echo = _to_wiki(_MD)
    mutations, _suppressed = compute_inbound_mutations(
        {"JIRA-1": _jira(echo)},
        _BindingStore({"JIRA-1": "T-1"}),
        {"T-1": _local(_MD)},
        inbound_mapper=_IdentityInboundMapper(),
        outbound_mapper=_LossyOutboundMapper(),
    )
    desc_updates = [m for m in mutations if "description" in m.fields]
    assert desc_updates == []


def test_orchestrator_still_emits_a_genuine_jira_edit() -> None:
    """The orchestrator-level negative: a real Jira-side edit still yields a mutation."""
    edited = _to_wiki(_MD) + "\nh2. An operator edit on the Jira side\n"
    mutations, _suppressed = compute_inbound_mutations(
        {"JIRA-1": _jira(edited)},
        _BindingStore({"JIRA-1": "T-1"}),
        {"T-1": _local(_MD)},
        inbound_mapper=_IdentityInboundMapper(),
        outbound_mapper=_LossyOutboundMapper(),
    )
    desc_updates = [m for m in mutations if "description" in m.fields]
    assert len(desc_updates) == 1


def test_without_outbound_mapper_the_legacy_adf_only_compare_is_unchanged() -> None:
    """Direct callers that omit the outbound port keep the legacy raw/ADF-only behaviour:
    with no landed-form compare, a Markdown-vs-wiki pair still reads as changed."""
    echo = _to_wiki(_MD)
    changed = _diff_jira_vs_local(
        _jira(echo),
        _local(_MD),
        binding_store=None,
        inbound_mapper=_IdentityInboundMapper(),
        outbound_mapper=None,
    )
    assert "description" in changed
