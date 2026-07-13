"""Exhaustive unit shard for the pure push-policy classifier.

This is the mutation-testing kill-suite for ``src/rebar/_store/_push_policy.py``
(story 25aa): it pins every branch, constant, and string in ``normalize_push_mode``
so a mutmut mutation of any of them is caught. See docs/mutation-testing.md.
"""

from __future__ import annotations

import pytest

from rebar._store._push_policy import normalize_push_mode


@pytest.mark.unit
class TestNormalizePushMode:
    # ── the three canonical values pass through verbatim ────────────────────────
    def test_always(self):
        assert normalize_push_mode("always") == "always"

    def test_async(self):
        assert normalize_push_mode("async") == "async"

    def test_off(self):
        assert normalize_push_mode("off") == "off"

    # ── case- and whitespace-insensitivity ─────────────────────────────────────
    @pytest.mark.parametrize(
        "raw,expect",
        [
            ("OFF", "off"),
            ("Off", "off"),
            (" off ", "off"),
            (" OFF ", "off"),
            ("\toff\n", "off"),
            ("ASYNC", "async"),
            (" Async ", "async"),
            ("ALWAYS", "always"),
            (" always ", "always"),
        ],
    )
    def test_case_and_space_insensitive(self, raw, expect):
        assert normalize_push_mode(raw) == expect

    # ── None / empty / unknown all default to "always" ─────────────────────────
    def test_none_defaults_to_always(self):
        assert normalize_push_mode(None) == "always"

    def test_empty_string_defaults_to_always(self):
        assert normalize_push_mode("") == "always"

    def test_whitespace_only_defaults_to_always(self):
        assert normalize_push_mode("   ") == "always"

    @pytest.mark.parametrize(
        "raw",
        ["on", "true", "1", "yes", "sync", "asynchronous", "of", "offf", "disable", "bogus"],
    )
    def test_unknown_defaults_to_always(self, raw):
        # Guards the membership check + the "always" fallback constant: any value
        # not exactly one of the three modes (after strip/lower) becomes "always",
        # never the raw value and never one of the other modes.
        assert normalize_push_mode(raw) == "always"

    def test_result_is_never_the_raw_unknown_value(self):
        # Distinguishes the "always" default from a pass-through mutant that would
        # return the input unchanged.
        assert normalize_push_mode("weird") != "weird"
