"""State-based echo suppression for the lossy rich-text wire (story 3388, epic 708d).

The DC codec is ONE-WAY and lossy, so loop prevention cannot rest on
``decode(encode(x)) == x``. Echo is suppressed by STATE instead: the local body is
compared against the last-synced baseline (ADR 0026).

The failure this guards against is subtle and permanent. The baseline holds
``decode(baseline_wire)`` — for DC, the RENDERED wiki — while the local body is raw
Markdown. Comparing only the raw forms, they differ on every pass, so the differ
reports "local changed" forever and re-emits an identical update each reconcile.

The fix is to let the baseline compare match on EITHER the raw local body or the local
body as it will READ once Jira stores it (``normalize_outbound(fit_outbound(local))``,
from the outbound port). That can only ADD a way to conclude "unchanged", so the
plain-codec behaviour is untouched — which is why the whole existing differ suite
passes unmodified.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar_reconciler.adapters.jira_family.rich_text import WikiTextCodec
from rebar_reconciler.outbound_field_diff import diff_canonical_fields

_MD = "# Heading\n\nProse with **bold**.\n\n- alpha\n- beta\n"


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


def _diff(local: str, remote: str, baseline: str | None, *, rich: bool) -> dict[str, Any]:
    """Run the real differ for a description-only ticket."""
    return diff_canonical_fields(
        {"title": "t", "description": local, "priority": 2, "status": "open"},
        {"title": "t", "description": remote, "priority": 2, "status": "open"},
        None if baseline is None else {"title": "t", "description": baseline},
        outbound_mapper=_Mapper(WikiTextCodec(rich=rich)),
    )


pytestmark = pytest.mark.skipif(
    _rendered("# x\n") == "# x\n",
    reason="pandoc unavailable, so the rich wire is the identity and the lossy case cannot arise",
)


def test_lossy_baseline_compare_local_normalized() -> None:
    """A converged body must NOT re-emit, even though raw local != baseline.

    This is the whole point: the baseline carries rendered wiki, the local body carries
    Markdown. Without matching on the landed form the differ re-emits forever.
    """
    wire = _rendered(_MD)

    changed = _diff(_MD, wire, wire, rich=True)

    assert "description" not in changed


def test_lossy_body_converges_across_passes() -> None:
    """Consecutive passes over an unedited body emit nothing, repeatedly."""
    wire = _rendered(_MD)

    for _ in range(5):
        assert "description" not in _diff(_MD, wire, wire, rich=True)


def test_a_genuine_local_edit_still_emits() -> None:
    """The suppression must not swallow a real change."""
    wire = _rendered(_MD)
    edited = _MD + "\nA newly added paragraph.\n"

    changed = _diff(edited, wire, wire, rich=True)

    assert "description" in changed
    assert "newly added paragraph" in changed["description"]


def test_legacy_body_upgrades_once() -> None:
    """A pre-cutover body upgrades exactly once, then converges.

    "Legacy" here means the remote still holds the un-rendered body and the baseline
    does NOT already record it as synced. The first pass emits the rendered form; once
    that has landed and become the baseline, the body is quiet forever.
    """
    first = _diff(_MD, _MD, None, rich=True)
    assert "description" in first
    upgraded = first["description"]
    assert upgraded == _rendered(_MD)

    # After the upgrade lands, the same local body is quiet.
    assert "description" not in _diff(_MD, upgraded, upgraded, rich=True)


def test_cutover_does_not_mass_rewrite_already_synced_bodies() -> None:
    """Flipping the flag must not re-emit every previously-synced description.

    When the baseline already records the body as synced (local == baseline), the
    ADR-0026 directionality guard defers to inbound and nothing is emitted. That keeps
    the cutover cheap and non-destructive: bodies migrate to the rich wire as they are
    genuinely edited, rather than the whole project being rewritten the moment the flag
    is turned on.
    """
    assert "description" not in _diff(_MD, _MD, _MD, rich=True)


def test_plain_codec_behaviour_is_unchanged() -> None:
    """With the flag off nothing about the compare changes."""
    assert "description" not in _diff(_MD, _MD, _MD, rich=False)
    assert "description" in _diff(_MD + "\nedit\n", _MD, _MD, rich=False)


def test_no_baseline_still_emits_on_difference() -> None:
    """A binding with no recorded baseline is partial-tolerant, not suppressed."""
    changed = _diff(_MD, "something else entirely", None, rich=True)

    assert "description" in changed
