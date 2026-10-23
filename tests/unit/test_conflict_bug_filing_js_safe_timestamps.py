"""Keep conflict-marker deduplication across JSON-safe timestamp forms.

``rebar show --output json`` emits large nanosecond instants as exact decimal
strings. The reconciler must parse those strings so its 24-hour suppression
window does not append another accumulation comment on each pass."""

from __future__ import annotations

import json

import pytest

from rebar.reconciler import conflict_bug_filing as cbf

pytestmark = pytest.mark.unit

#: RFC 8259 section 6 interoperable integer range; also JS ``Number.MAX_SAFE_INTEGER``.
JS_SAFE_MAX = 2**53 - 1

#: A real 19-digit ns instant, comfortably outside the JS-safe range.
_NOW_EPOCH = 1787860170
_FRESH_NS = 1787860170488898642


def _runner_returning(comments: list[dict]) -> cbf.Runner:
    """An injected runner whose ``show`` returns exactly ``comments``."""

    def runner(_argv: list[str]) -> tuple[int, str, str]:
        return 0, json.dumps({"ticket_id": "abcd-1234-5678-9abc", "comments": comments}), ""

    return runner


def test_string_wire_form_is_recognized_inside_the_window() -> None:
    """The REPORTED mechanism: a decimal-string ns timestamp must still match the window."""
    assert _FRESH_NS > JS_SAFE_MAX, (
        "the fixture instant is inside the JS-safe range, so it would never be emitted as a "
        "string and this test could not detect the defect"
    )
    comments = [{"body": f"{cbf._MARKER} conflict persisted", "timestamp": str(_FRESH_NS)}]
    assert f'"timestamp": "{_FRESH_NS}"' in json.dumps({"comments": comments}), (
        "the fixture must genuinely arrive as a JSON string, not a number"
    )
    runner = _runner_returning(comments)

    assert cbf._recent_marker_comment("rebar", "abcd-1234-5678-9abc", runner, _NOW_EPOCH) is True, (
        "a string-form timestamp was not recognized, so the 24h duplicate-suppression "
        "window silently stopped working"
    )


def test_integer_wire_form_still_works() -> None:
    """Backward compatibility: the pre-e127 integer form must keep matching."""
    runner = _runner_returning(
        [{"body": f"{cbf._MARKER} conflict persisted", "timestamp": _FRESH_NS}]
    )

    assert cbf._recent_marker_comment("rebar", "abcd-1234-5678-9abc", runner, _NOW_EPOCH) is True


def test_a_stale_string_timestamp_does_not_suppress() -> None:
    """LIVENESS: the function must still return False outside the window.

    Without this, an implementation that simply returned ``True`` would pass the two tests
    above, and the suppression window would be vacuously 'working'.
    """
    stale_ns = (_NOW_EPOCH - cbf._ACCUMULATION_WINDOW_SECS - 3600) * 1_000_000_000
    assert stale_ns > JS_SAFE_MAX
    runner = _runner_returning(
        [{"body": f"{cbf._MARKER} old conflict", "timestamp": str(stale_ns)}]
    )

    assert cbf._recent_marker_comment("rebar", "abcd-1234-5678-9abc", runner, _NOW_EPOCH) is False


def test_a_non_numeric_string_is_skipped_not_raised() -> None:
    """Fail-soft is preserved: junk must not raise out of a best-effort filer."""
    runner = _runner_returning([{"body": f"{cbf._MARKER} junk", "timestamp": "not-a-number"}])

    assert cbf._recent_marker_comment("rebar", "abcd-1234-5678-9abc", runner, _NOW_EPOCH) is False
