"""Ticket ``65d7``: the assignee resolver travels by CONTRACT, not by attribute injection.

The outbound mapper used to receive its live account-search resolver as an attribute set on
it from outside (``outbound_mapper._assignee_resolver = ...``, under a bare
``except (AttributeError, TypeError): pass``), which each backend then rediscovered with
``getattr(self, "_assignee_resolver", None)``. Three costs, all load-bearing:

* it fails SILENTLY — an injection that does not happen degrades every resolution to
  ``authoritative=False``, and a permanently non-authoritative assignee re-emits an outbound
  change it can never converge (the churn class epic ``ace2`` exists to fix);
* it hid a real bug in review — PR #120's Data Center adapter copied the ``getattr`` into a
  class where nothing set the attribute, so its whole authoritative branch was dead code;
* it is invisible to mypy, so each wiring site needed ``# type: ignore[attr-defined]``.

These tests pin the replacement: a declared, keyword-only ``assignee_resolver`` parameter on
the neutral ``OutboundMapper.resolve_assignee`` contract and on both implementers, threaded
from the core diff. The structural test at the bottom is what makes the defect class
UNWRITABLE rather than merely fixed once.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest
from _tree_scan import ParsedModule, parsed_python_files

from rebar_reconciler import outbound_field_diff as _ofd
from rebar_reconciler._backend import OutboundMapper
from rebar_reconciler.adapters.jira.backend import JiraBackend
from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend

from .backend_support import FakeTransport

_REC = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine" / "rebar_reconciler"

#: An authoritative live lookup: "ada@example.com" is account ``acct-1``.
_RESOLVED: tuple[Any, bool, bool] = ("acct-1", True, True)


def _resolver(result: tuple[Any, bool, bool] = _RESOLVED):
    def resolve(_local_value: str) -> tuple[Any, bool, bool]:
        return result

    return resolve


# ---------------------------------------------------------------------------
# 1. The contract declares the collaborator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    [
        OutboundMapper.resolve_assignee,
        JiraBackend(transport=FakeTransport()).outbound.resolve_assignee,
        JiraDataCenterBackend(transport=FakeTransport()).outbound.resolve_assignee,
    ],
    ids=["port", "cloud", "data-center"],
)
def test_resolve_assignee_declares_the_resolver_parameter(method) -> None:
    """The neutral port AND both implementers take the resolver by name.

    A defaulted parameter on the port alone would not be enough: if only one implementer
    accepted it the other's authoritative branch would go dead — PR #120's exact defect.
    """
    param = inspect.signature(method).parameters.get("assignee_resolver")
    assert param is not None, f"{method} does not declare assignee_resolver"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, param.kind
    assert param.default is None, param.default


# ---------------------------------------------------------------------------
# 2. Passing the resolver explicitly resolves authoritatively (both backends)
# ---------------------------------------------------------------------------


def test_cloud_resolves_authoritatively_from_the_explicit_parameter() -> None:
    outbound = JiraBackend(transport=FakeTransport()).outbound
    assert outbound.resolve_assignee("ada@example.com", None, assignee_resolver=_resolver()) == (
        "acct-1",
        True,
        True,
    )


def test_data_center_resolves_authoritatively_from_the_explicit_parameter() -> None:
    outbound = JiraDataCenterBackend(transport=FakeTransport()).outbound
    value, authoritative, is_account_id = outbound.resolve_assignee(
        "ada", None, assignee_resolver=_resolver(("ada.l", True, False))
    )
    assert (value, authoritative, is_account_id) == ("ada.l", True, False)


# ---------------------------------------------------------------------------
# 3. Absence is an explicit, documented default — not a silent degradation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outbound",
    [
        JiraBackend(transport=FakeTransport()).outbound,
        JiraDataCenterBackend(transport=FakeTransport()).outbound,
    ],
    ids=["cloud", "data-center"],
)
def test_omitting_the_resolver_is_the_documented_permissive_default(outbound) -> None:
    """``assignee_resolver=None`` keeps its existing meaning — "no live account search on
    this path" — and yields the permissive, non-authoritative string match. The VALUE is
    unchanged from the injection era; what changed is that a caller now *chooses* it
    rather than inheriting it from an injection that silently did not happen."""
    assert outbound.resolve_assignee("ada@example.com", {"account_id": "acct-9"}) == (
        "ada@example.com",
        False,
        False,
    )


# ---------------------------------------------------------------------------
# 4. Data Center's constructor-bound resolver, and core precedence over it
# ---------------------------------------------------------------------------


def test_data_center_uses_its_constructor_supplied_resolver() -> None:
    """DC binds a client-backed resolver once at construction (no ``jira_key``). Under the
    explicit contract that arrives as a declared constructor parameter, not an attribute."""
    backend = JiraDataCenterBackend(transport=FakeTransport(), client=object())
    backend.outbound = type(backend.outbound)(assignee_resolver=_resolver(("ada.l", True, False)))
    assert backend.outbound.resolve_assignee("ada", None) == ("ada.l", True, False)


def test_an_explicit_resolver_wins_over_the_constructor_one() -> None:
    """Preserves today's semantics EXACTLY: the core injection used to overwrite DC's
    constructor-set attribute whenever the differ ran, so the core-supplied resolver wins."""
    outbound = type(JiraDataCenterBackend(transport=FakeTransport()).outbound)(
        assignee_resolver=_resolver(("from-constructor", True, False))
    )
    assert outbound.resolve_assignee(
        "ada", None, assignee_resolver=_resolver(("from-core", True, False))
    ) == ("from-core", True, False)


# ---------------------------------------------------------------------------
# 5. The core threads it all the way to resolve_assignee
# ---------------------------------------------------------------------------


class _RecordingOutbound:
    """A mapper that is NOT attribute-injectable — the failure mode the old
    ``try/except (AttributeError, TypeError): pass`` swallowed."""

    __slots__ = ("seen",)

    def __init__(self) -> None:
        self.seen: list[Any] = []

    def map_fields_to_remote(self, changed, ticket=None, binding_store=None, **_kw):
        return dict(changed)

    def resolve_assignee(self, local_value, remote_identity, *, assignee_resolver=None):
        self.seen.append(assignee_resolver)
        if assignee_resolver is None:
            return (local_value, False, False)
        return assignee_resolver(local_value)


class _PassthroughInbound:
    def map_remote_to_local(self, remote_fields):
        return dict(remote_fields or {})


def test_compute_update_fields_threads_the_resolver_to_the_mapper() -> None:
    """End-to-end through the real core helper: the resolver reaches ``resolve_assignee``
    bound to the current remote key, WITHOUT any attribute being set on the mapper."""
    outbound = _RecordingOutbound()
    calls: list[tuple[str, str]] = []

    def core_resolver(local_value: str, jira_key: str) -> tuple[Any, bool, bool]:
        calls.append((local_value, jira_key))
        return ("acct-1", True, True)

    fields = _ofd.compute_update_fields(
        {"assignee": "ada@example.com", "title": "T"},
        {"assignee": "someone-else"},
        inbound_mapper=_PassthroughInbound(),
        outbound_mapper=outbound,
        jira_key="REB-1",
        assignee_resolver=core_resolver,
    )

    assert outbound.seen and outbound.seen[0] is not None, "the resolver never arrived"
    assert calls == [("ada@example.com", "REB-1")], calls
    assert fields.get("assignee") == "acct-1", fields


# ---------------------------------------------------------------------------
# 6. Structural: the defect class is unwritable, not merely absent
# ---------------------------------------------------------------------------


def _reconciler_sources() -> tuple[ParsedModule, ...]:
    return parsed_python_files(_REC)


def test_no_module_discovers_the_resolver_by_getattr() -> None:
    """No ``getattr(x, "_assignee_resolver", ...)`` anywhere in the reconciler."""
    offenders: list[str] = []
    for module in _reconciler_sources():
        for node in ast.walk(module.tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "_assignee_resolver"
            ):
                offenders.append(f"{module.path.name}:{node.lineno}")
    assert not offenders, f"resolver still discovered by getattr at {offenders}"


def test_no_module_injects_the_resolver_as_an_attribute() -> None:
    """No ``<something>._assignee_resolver = ...`` anywhere in the reconciler.

    ``outbound_differ`` also defines a nested FUNCTION named ``_assignee_resolver``; that is
    an unrelated local, not a collaborator smuggled onto an object, so it is untouched — this
    check is deliberately keyed on attribute ASSIGNMENT rather than the bare name.
    """
    offenders: list[str] = []
    for module in _reconciler_sources():
        for node in ast.walk(module.tree):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "_assignee_resolver":
                    offenders.append(f"{module.path.name}:{node.lineno}")
    assert not offenders, f"resolver still injected as an attribute at {offenders}"


def test_no_attr_defined_type_ignore_remains_for_the_resolver() -> None:
    """The dependency is on the contract now, so mypy can see it — no suppression needed."""
    offenders = [
        f"{module.path.name}:{i}"
        for module in _reconciler_sources()
        for i, line in enumerate(module.source.splitlines(), start=1)
        if "_assignee_resolver" in line and "type: ignore[attr-defined]" in line
    ]
    assert not offenders, offenders
