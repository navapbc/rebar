"""Keep every advisory finding retrievable on its Gerrit change.

PASS and BLOCK messages enumerate advisory text. Findings anchored to revision
paths also become inline comments, while unanchored findings remain only in the
message. If posting comments fails, the bot falls back to a message-only vote
that names the publishing failure.
"""

from __future__ import annotations

import pytest

from rebar.review_bot import adapter, finding_publish
from rebar.review_bot.gerrit_client import GerritError


def _finding(detail: str, *, criteria: str = "quality", location: str | None = None) -> dict:
    f: dict = {"finding": detail, "criteria": [criteria], "severity": "minor"}
    if location is not None:
        f["location"] = location
    return f


# AC4(a): PASS advisories.


def test_pass_message_enumerates_advisory_finding_text() -> None:
    """RED against pre-fix main, which emitted only the count."""
    verdict = {
        "verdict": "PASS",
        "advisory": [
            _finding("the retry budget is never reset on success", location="src/rebar/a.py:12"),
            _finding("this helper duplicates an existing one"),
        ],
    }
    message = adapter._summarize("PASS", verdict)

    assert "2 advisory finding(s) (non-blocking)" in message
    assert "the retry budget is never reset on success" in message
    assert "this helper duplicates an existing one" in message
    assert "[src/rebar/a.py:12]" in message


def test_pass_message_is_unchanged_when_there_are_no_advisories() -> None:
    message = adapter._summarize("PASS", {"verdict": "PASS", "advisory": []})
    assert message == "rebar code review passed."


# AC4(b): BLOCK advisories.


def test_block_message_enumerates_both_blocking_and_advisory_findings() -> None:
    verdict = {
        "verdict": "BLOCK",
        "blocking": [_finding("unbounded read of an untrusted body", criteria="security")],
        "advisory": [_finding("the docstring contradicts the signature")],
    }
    message = adapter._block("finding", verdict)["message"]

    assert "unbounded read of an untrusted body" in message
    assert "the docstring contradicts the signature" in message


def test_blocking_enumeration_keeps_its_pre_fix_shape() -> None:
    """The extraction into render_findings_block must not alter the blocking block's wording."""
    verdict = {"blocking": [_finding("a real problem", criteria="security")]}
    message = adapter._summarize("finding", verdict)

    assert message.splitlines()[0] == "rebar code review found 1 blocking issue(s):"
    assert message.splitlines()[1] == "- (security) a real problem"


def test_overflow_note_counts_the_findings_beyond_the_cap() -> None:
    findings = [_finding(f"issue number {i}") for i in range(finding_publish.MAX_ITEMS + 3)]
    block = finding_publish.render_findings_block(findings, kind="advisory")

    assert "issue number 0" in block
    assert f"issue number {finding_publish.MAX_ITEMS}" not in block
    assert "3 additional advisory findings omitted from this summary." in block


def test_a_long_detail_is_truncated_but_still_present() -> None:
    block = finding_publish.render_findings_block([_finding("x" * 500)], kind="advisory")
    assert "…" in block
    assert "x" * finding_publish.MAX_DETAIL_CHARS not in block
    assert "x" * 100 in block


# AC4(c,d): anchors.


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("src/rebar/voter.py:700", ("src/rebar/voter.py", 700)),
        ("src/rebar/voter.py:700-712", ("src/rebar/voter.py", 700)),
        ("src/rebar/voter.py", ("src/rebar/voter.py", None)),
        ("the whole module:somewhere", ("the whole module:somewhere", None)),
        ("", None),
        (None, None),
    ],
)
def test_parse_anchor_reads_a_free_text_location(location, expected) -> None:
    assert finding_publish.parse_anchor(location) == expected


def test_parse_anchor_agrees_with_the_review_result_location_parser() -> None:
    """Reconciliation with the pre-existing authority, ``shim._parse_location`` — the two rules
    must not drift apart (they read the SAME free-text ``location`` field)."""
    from rebar.llm.code_review.shim import _parse_location

    for loc in ("a/b.py:12", "a/b.py:12-20", "a/b.py", "not a path:x", "", "   "):
        peer = _parse_location(loc)
        mine = finding_publish.parse_anchor(loc)
        assert mine == (None if peer[0] is None else peer), loc


def test_an_anchorable_finding_becomes_an_inline_comment() -> None:
    findings = [_finding("off-by-one here", location="src/rebar/a.py:12")]
    anchorable, anchorless = finding_publish.partition_findings(findings, {"src/rebar/a.py"})

    assert anchorless == []
    comments = finding_publish.build_inline_comments(anchorable)
    assert set(comments) == {"src/rebar/a.py"}
    entry = comments["src/rebar/a.py"][0]
    assert entry["line"] == 12
    assert entry["unresolved"] is False
    assert "off-by-one here" in entry["message"]


def test_an_off_revision_or_anchorless_finding_is_never_inlined() -> None:
    findings = [
        _finding("no location at all"),
        _finding("a path that is not in this revision", location="src/rebar/other.py:3"),
    ]
    anchorable, anchorless = finding_publish.partition_findings(findings, {"src/rebar/a.py"})

    assert anchorable == []
    assert len(anchorless) == 2
    assert finding_publish.build_inline_comments(anchorable) == {}

    block = finding_publish.render_findings_block(anchorless, kind="advisory")
    assert "no location at all" in block
    assert "a path that is not in this revision" in block


# AC4(e): publishing failures.


class _FakeGerrit:
    """Records post_vote calls; optionally fails the comment-bearing one."""

    def __init__(self, *, files: dict | Exception, fail_with: GerritError | None = None) -> None:
        self._files = files
        self._fail_with = fail_with
        self.calls: list[dict] = []

    def get_revision_files(self, change_id: str, revision: str) -> dict:
        if isinstance(self._files, Exception):
            raise self._files
        return self._files

    def post_vote(self, change_id, revision, value, message, comments=None):
        self.calls.append({"message": message, "comments": comments})
        if comments and self._fail_with is not None:
            raise self._fail_with
        return 200


def _findings() -> list[dict]:
    return [_finding("anchored problem", location="src/rebar/a.py:5")]


def test_a_failed_comment_post_falls_back_to_a_message_only_vote_with_a_notice() -> None:
    gc = _FakeGerrit(files={"src/rebar/a.py": {}}, fail_with=GerritError("bad request", status=400))
    # The adapter's real output: the finding text is ALREADY in the body.
    body = (
        "rebar code review passed. 1 advisory finding(s) (non-blocking):\n"
        "- (quality) anchored problem [src/rebar/a.py:5]"
    )

    status = finding_publish.post_review(gc, "I123", "rev1", 1, body, _findings())

    assert status == 200
    assert len(gc.calls) == 2, "expected a comment-bearing attempt then a message-only retry"
    assert gc.calls[0]["comments"], "the first attempt must carry the inline comments"
    assert gc.calls[1]["comments"] in (None, {}), "the retry must be message-only"

    retry_message = gc.calls[1]["message"]
    assert retry_message.startswith(body), "the enumerated findings survive the retry verbatim"
    assert "could not be published" in retry_message
    assert "code_review artifact" in retry_message
    assert "anchored problem" in retry_message


def test_a_closed_change_still_propagates_so_the_voter_can_skip_it() -> None:
    gc = _FakeGerrit(files={"src/rebar/a.py": {}}, fail_with=GerritError("closed", status=409))

    with pytest.raises(GerritError) as excinfo:
        finding_publish.post_review(gc, "I123", "rev1", 1, "msg", _findings())

    assert excinfo.value.status == 409
    assert len(gc.calls) == 1, "a 409 is terminal — no retry"


def test_an_unreadable_file_map_degrades_to_a_body_only_vote() -> None:
    gc = _FakeGerrit(files=RuntimeError("network down"))

    status = finding_publish.post_review(gc, "I123", "rev1", 1, "msg", _findings())

    assert status == 200
    assert len(gc.calls) == 1
    assert not gc.calls[0]["comments"], "no anchors could be validated, so nothing is inlined"


def test_the_translated_finding_shape_the_voter_passes_still_renders() -> None:
    """The voter hands post_review ``decision["findings"]`` — the TRANSLATED shape
    (``{severity, dimension, detail, location}``), not the raw kernel one. Reading only the raw
    keys produced inline comments with empty text."""
    translated = [
        {
            "severity": "medium",
            "dimension": "quality",
            "detail": "the retry budget is never reset",
            "location": "src/rebar/a.py:9",
        }
    ]
    anchorable, anchorless = finding_publish.partition_findings(translated, {"src/rebar/a.py"})
    assert anchorless == []

    entry = finding_publish.build_inline_comments(anchorable)["src/rebar/a.py"][0]
    assert entry["message"] == "(quality) the retry budget is never reset"
    assert "the retry budget is never reset" in finding_publish.render_findings_block(
        translated, kind="advisory"
    )


def test_a_review_with_no_findings_posts_exactly_as_before() -> None:
    gc = _FakeGerrit(files={"src/rebar/a.py": {}})

    status = finding_publish.post_review(gc, "I123", "rev1", 1, "clean", [])

    assert status == 200
    assert gc.calls == [{"message": "clean", "comments": None}]
