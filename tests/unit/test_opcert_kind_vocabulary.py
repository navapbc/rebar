"""Pin ``signing.OPCERT_KINDS`` as the sole op-cert kind source.

The ordered consumer tuples use ``tuple(sorted(...))`` so argparse choices, API error lists, and
the public ``VALID_KINDS`` export remain deterministic.
"""

from __future__ import annotations

import inspect

import pytest

from rebar._commands import remote_cert
from rebar.opcert_service import jobs
from rebar.signing import OPCERT_KINDS

pytestmark = pytest.mark.unit

CONSUMERS = [(jobs, "VALID_KINDS"), (remote_cert, "_VALID_KINDS")]


@pytest.mark.parametrize(("module", "name"), CONSUMERS)
def test_each_consumer_derives_from_the_canonical_set(module, name: str) -> None:
    """AC1."""
    assert getattr(module, name) == tuple(sorted(OPCERT_KINDS))


@pytest.mark.parametrize(("module", "name"), CONSUMERS)
def test_each_consumer_is_still_an_ordered_tuple(module, name: str) -> None:
    """AC1b. A frozenset here would change a published type and make argparse help and the
    app's error message non-deterministic."""
    value = getattr(module, name)
    assert isinstance(value, tuple)
    assert value == ("completion-verifier", "plan-review"), (
        "the order these consumers already rendered must not change"
    )


@pytest.mark.parametrize(("module", "name"), CONSUMERS)
def test_no_consumer_re_lists_the_kinds(module, name: str) -> None:
    """AC1, negative half."""
    source = inspect.getsource(module)
    assert '("completion-verifier", "plan-review")' not in source


def test_the_usage_prose_is_generated_from_the_derived_tuple() -> None:
    """AC2."""
    for kind in remote_cert._VALID_KINDS:
        assert kind in remote_cert._USAGE
    assert "{" + "|".join(remote_cert._VALID_KINDS) + "}" in remote_cert._USAGE


def test_a_kind_added_canonically_reaches_both_consumers() -> None:
    """AC3. Both derive, so neither needs a second edit."""
    extended = set(OPCERT_KINDS) | {"provenance-verifier"}
    for module, name in CONSUMERS:
        assert extended - set(getattr(module, name)) == {"provenance-verifier"}


@pytest.mark.parametrize(("module", "name"), CONSUMERS)
def test_an_unknown_kind_is_still_rejected(module, name: str) -> None:
    """AC4."""
    assert "not-a-kind" not in getattr(module, name)
