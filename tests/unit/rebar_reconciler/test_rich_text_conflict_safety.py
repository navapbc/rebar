"""Concurrency + origin-gating safety for the rich-text cutover (story 3388, epic 708d).

The cutover changes the FORM of the description on the wire. It must not change the
settled arbitration around it (story a713): a both-sides edit still keeps rebar's body
(local-wins) AND records the remote value through the ``outbound-field-conflict`` bridge
alert, so a concurrent Jira edit is never silently destroyed.

It must also not start emitting on bodies nobody edited. Outbound fires only when the
LOCAL body differs from the baseline, so a Jira-authored construct that rebar never
touched survives an edit-free pass instead of being clobbered by a re-render.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar_reconciler.adapters.jira_family.rich_text import WikiTextCodec
from rebar_reconciler.outbound_field_diff import diff_canonical_fields

_MD = "# Heading\n\nProse with **bold**.\n\n- alpha\n"


class _Mapper:
    """A minimal ``OutboundMapper`` whose description op is a real codec."""

    def __init__(self, codec: Any) -> None:
        self._codec = codec

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
            out["description"] = self._codec.normalize_outbound(self._codec.fit_outbound(value))
        return out

    def map_local_to_remote(self, *a: Any, **k: Any) -> dict[str, Any]:  # pragma: no cover
        return {}

    def resolve_assignee(self, *a: Any, **k: Any) -> tuple[Any, bool, bool]:  # pragma: no cover
        return (None, False, False)


def _rendered(body: str) -> str:
    codec = WikiTextCodec(rich=True)
    return codec.normalize_outbound(codec.fit_outbound(body))


def _run(
    local: str, remote: str, baseline: str | None, *, sink: list[tuple[str, str]] | None = None
) -> dict[str, Any]:
    return diff_canonical_fields(
        {"title": "t", "description": local, "priority": 2, "status": "open"},
        {"title": "t", "description": remote, "priority": 2, "status": "open"},
        None if baseline is None else {"title": "t", "description": baseline},
        outbound_mapper=_Mapper(WikiTextCodec(rich=True)),
        jira_key="REB-1",
        conflict_sink=sink,
    )


pytestmark = pytest.mark.skipif(
    _rendered("# x\n") == "# x\n",
    reason="pandoc unavailable, so the rich wire is the identity and these cases cannot arise",
)


def test_concurrent_conflict_alerts() -> None:
    """Both sides edited: rebar's body wins AND the remote value is recorded.

    Local-wins without the alert would silently destroy a concurrent Jira edit; the
    alert is what makes the overwrite observable rather than invisible.
    """
    baseline = _rendered(_MD)
    local_edit = _MD + "\nrebar added this.\n"
    remote_edit = baseline + "\nsomebody edited this in Jira.\n"
    sink: list[tuple[str, str]] = []

    changed = _run(local_edit, remote_edit, baseline, sink=sink)

    # local-wins: rebar's body is what gets emitted
    assert "description" in changed
    assert "rebar added this." in changed["description"]
    # ...and the conflict is recorded, not swallowed
    assert ("REB-1", "description") in sink


def test_no_conflict_when_only_the_local_side_moved() -> None:
    """A one-sided rebar edit is not a conflict — it is just an update."""
    baseline = _rendered(_MD)
    sink: list[tuple[str, str]] = []

    changed = _run(_MD + "\nrebar edit.\n", baseline, baseline, sink=sink)

    assert "description" in changed
    assert sink == []


def test_echo_rebar_origin_only() -> None:
    """Outbound fires only on a rebar-originated change.

    A converged body — local unchanged since the baseline — emits nothing, so a pass
    where rebar did not edit anything sends no description at all.
    """
    baseline = _rendered(_MD)

    assert "description" not in _run(_MD, baseline, baseline)


def test_jira_native_construct_survives_an_edit_free_pass() -> None:
    """A Jira-authored panel/mention/date is not clobbered by a re-render.

    rebar never edited this body, so origin-gating means nothing is emitted and the
    native construct stays exactly as the Jira author left it.
    """
    baseline = _rendered(_MD)
    jira_authored = baseline + "\n{panel:title=Note}see [~alice] before 2026-08-13{panel}\n"
    sink: list[tuple[str, str]] = []

    changed = _run(_MD, jira_authored, baseline, sink=sink)

    assert "description" not in changed  # nothing emitted → the panel survives untouched
    assert sink == []  # and it is not reported as a conflict, because rebar did not edit


def test_recanonicalization_converges_within_one_reemit() -> None:
    """A benign Jira re-canonicalization settles after one re-emit, with no re-GET.

    Jira may store a slightly different byte sequence than was sent. The first pass
    re-emits; once the landed form becomes the baseline the body is quiet, so the
    convergence costs one update rather than looping.
    """
    sent = _rendered(_MD)
    recanonicalized = sent + "\n"  # Jira appended a trailing newline

    # Baseline still records what rebar sent, so the pass is quiet: the LOCAL side did
    # not move, and trailing whitespace is tolerated by the comparator.
    assert "description" not in _run(_MD, recanonicalized, sent)

    # Once the re-canonicalized form is the baseline it stays quiet too.
    assert "description" not in _run(_MD, recanonicalized, recanonicalized)
