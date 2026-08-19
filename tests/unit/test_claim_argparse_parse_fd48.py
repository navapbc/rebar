"""Parser-level fidelity for claim's argparse-backed arg parsing (ticket churlish/fd48).

The claim command retired its hand-rolled ``--assignee`` loop for stdlib argparse
(``rebar._commands._seam.write_arg_parser``). These tests pin the load-bearing behaviors
argparse does NOT reproduce by default, so a future default-behavior regression fails loudly:
the ``--assignee`` None-vs-"" sentinel, value-less exit-1 with the historical message,
exact-spelling matching (``allow_abbrev=False``), silent skipping of unknown tokens
(``parse_known_args``), and claim's command-specific ``--force`` following-token rule.
"""

from __future__ import annotations

import pytest

from rebar._commands._seam import CommandError
from rebar._commands.claim import _parse_assignee, _parse_force


class TestParseAssignee:
    def test_absent_is_none_sentinel(self) -> None:
        # ABSENT -> None (drives the ticket.default_assignee fallback), distinct from "".
        assert _parse_assignee([]) is None
        assert _parse_assignee(["--force", "--review"]) is None

    def test_inline_value(self) -> None:
        assert _parse_assignee(["--assignee=alice"]) == "alice"

    def test_space_separated_value(self) -> None:
        assert _parse_assignee(["--assignee", "alice"]) == "alice"

    def test_explicit_empty_clears(self) -> None:
        # present-but-empty CLEARS (no fallback) -- must NOT collapse to the None sentinel.
        assert _parse_assignee(["--assignee", ""]) == ""
        assert _parse_assignee(["--assignee="]) == ""

    def test_value_less_at_end_exits_one(self) -> None:
        with pytest.raises(CommandError) as exc:
            _parse_assignee(["--assignee"])
        assert exc.value.returncode == 1
        assert exc.value.message == "Error: --assignee requires a value"

    def test_abbreviation_does_not_match(self) -> None:
        # allow_abbrev=False: a truncation stays an unknown token (skipped), NOT --assignee.
        assert _parse_assignee(["--assign", "alice"]) is None
        assert _parse_assignee(["--assignee-extra=bob"]) is None

    def test_unknown_tokens_skipped(self) -> None:
        assert _parse_assignee(["--force=x", "--assignee=bob", "--review"]) == "bob"


class TestParseForce:
    def test_absent_is_empty(self) -> None:
        assert _parse_force([]) == ""
        assert _parse_force(["--assignee=bob"]) == ""

    def test_inline_reason(self) -> None:
        assert _parse_force(["--force=gate offline"]) == "gate offline"

    def test_inline_empty_yields_default_note(self) -> None:
        assert _parse_force(["--force="]) == "(no reason given)"

    def test_bare_consumes_following_non_dashdash_token(self) -> None:
        assert _parse_force(["--force", "operator override"]) == "operator override"

    def test_bare_before_dashdash_token_is_default_note(self) -> None:
        assert _parse_force(["--force", "--review"]) == "(no reason given)"

    def test_bare_at_end_is_default_note(self) -> None:
        assert _parse_force(["--force"]) == "(no reason given)"
