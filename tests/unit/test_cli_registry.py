"""Happy-path contract for the RP-05 S2a route registry (``rebar._cli._registry``).

These tests pin the *observable* foundation contract the registry must satisfy as the
SOLE routing authority (RP-05 S6 completed the cutover — the router's duplicate literal
policy frozensets were retired):

* the immutable route table names every current spelling, and
* ``derive_policy_sets`` covers exactly the named policy-set census, and
* the shipped route table validates clean.

Adversarial validation, import-isolation, and parse-error shape live in the
held-out oracle and are exercised by the orchestrator, not the implementer.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from rebar._cli import _registry
from rebar._cli._registry import Route, validate

# The named policy sets ``derive_policy_sets`` produces — its complete key census. (Before
# RP-05 S6 the router also shipped these as literal ``_cli`` frozensets; that duplicate
# authority was retired, so the registry's derived output is now the single source.)
_CENSUS_SETS = (
    "_NO_AUTO_MOUNT",
    "_INTERCEPTS",
    "_READS_INIT_ONLY",
    "_READS_NO_INIT",
    "_FIELD_READS",
    "_LOOKUPS",
    "_DESCENDANTS",
    "_GATES",
    "_SIGNING",
    "_LIFECYCLE",
    "_COMPACT",
    "_BRIDGE",
    "_HIDDEN_ALIASES",
    "_IO",
    "_WRITES_FULL",
    "_CONFIRM_SCOPE",
    "_LEGACY_OUTPUT",
)

# Arms the router dispatches by an explicit ``if sub == ...`` rather than a
# frozenset, plus ``audit`` (a main() intercept that also carries pinned help).
# Every one of these must also be a known route so the census is complete.
_INDIVIDUAL_ARMS = frozenset(
    {
        "init",
        "scratch",
        "metrics",
        "delete",
        "fsck",
        "fsck-recover",
        "tracker-maintenance",
        "doctor",
        "bridge-probe",
        "grounding-info",
        "audit",
    }
)


def _all_current_spellings() -> frozenset[str]:
    # RP-05 S6: the route registry is the SOLE authority — the router's literal policy
    # frozensets were retired — so the spelling census is derived from the registry's own
    # ``derive_policy_sets`` output, not from ``_cli`` module attributes.
    derived = _registry.derive_policy_sets()
    grouped: frozenset[str] = frozenset()
    for name in _CENSUS_SETS:
        if name in ("_CONFIRM_SCOPE", "_LEGACY_OUTPUT", "_NO_AUTO_MOUNT"):
            # derived/overlay sets — their members already appear in a base set
            continue
        grouped |= derived[name]
    return grouped | _INDIVIDUAL_ARMS


def test_derive_covers_exactly_the_census_set_names() -> None:
    derived = _registry.derive_policy_sets()
    assert set(derived) == set(_CENSUS_SETS)


def test_every_current_spelling_has_a_route() -> None:
    missing = [s for s in sorted(_all_current_spellings()) if _registry.route_for(s) is None]
    assert missing == [], f"spellings with no route record: {missing}"


def test_hidden_ships_but_retired_bridge_tokens_do_not() -> None:
    # bridge-status is hidden (resolvable, undiscoverable): it must exist as a
    # route record so policy derives correctly.
    hidden = _registry.route_for("bridge-status")
    assert hidden is not None and hidden.hidden is True
    # Retired bridge verbs are erased from shipped source (the project's bridge
    # vocabulary contract), so the shipped table names none of them. The retired
    # mechanism itself is exercised on synthetic routes in the validation oracle.
    assert _registry.route_for("purge-bridge") is None
    assert all(not route.retired for route in _registry.ROUTES)


def test_shipped_route_table_validates_clean() -> None:
    assert tuple(_registry.validate()) == ()


def test_tracker_footprint_is_an_unmounted_read_only_intercept() -> None:
    route = _registry.route_for("tracker-footprint")

    assert route is not None
    assert route.group == "static_read"
    assert route.intercept is True
    assert route.no_auto_mount is True
    assert route.init == "none"
    assert route.capabilities == ()


def test_routes_are_immutable() -> None:
    route = _registry.route_for("show")
    assert route is not None
    # frozen dataclass -> FrozenInstanceError, which subclasses AttributeError
    with pytest.raises(AttributeError):
        route.name = "mutated"  # type: ignore[misc]


# --- Held-out oracle (adversarial validation + import isolation) ---
# Withheld from the S2a implementer; validated post-hoc by the orchestrator.
def _route(name: str, **kw) -> Route:
    kw.setdefault("group", "reads_init_only")
    return Route(name=name, **kw)


def _codes(routes) -> set[str]:
    return {f.code for f in validate(routes)}


def test_duplicate_spelling_rejected() -> None:
    assert "duplicate" in _codes([_route("show"), _route("show")])


def test_alias_duplicates_a_canonical_spelling_rejected() -> None:
    # an alias that collides with another route's canonical name is a duplicate spelling
    assert "duplicate" in _codes([_route("show"), _route("list", aliases=("show",))])


def test_same_alias_on_two_routes_rejected() -> None:
    # the SAME alias declared by two distinct routes is a duplicate spelling: at
    # dispatch time only one route could own it, so the table is ambiguous.
    assert "duplicate" in _codes([_route("show", aliases=("ls",)), _route("list", aliases=("ls",))])


def test_alias_repeated_within_one_route_rejected() -> None:
    # a route declaring the same alias twice in its own tuple is a duplicate spelling
    assert "duplicate" in _codes([_route("show", aliases=("ls", "ls"))])


def test_alias_retired_collision_rejected() -> None:
    routes = [
        _route("bridge", aliases=("purge-bridge",)),
        _route("purge-bridge", retired=True, group="bridge"),
    ]
    alias_codes = {f.code for f in validate(routes) if f.spelling == "purge-bridge"}
    assert "alias_retired_collision" in alias_codes
    # a retired spelling is NOT a live canonical name, so the alias must not ALSO
    # be double-reported as a plain 'duplicate' — only the retired-collision fires
    assert "duplicate" not in alias_codes


def test_unknown_capability_rejected() -> None:
    assert "unknown_capability" in _codes([_route("x", capabilities=("no-such-capability",))])


def test_known_capability_accepted() -> None:
    # a capability id in the known set does not raise the unknown_capability finding
    cap = next(iter(_registry.KNOWN_CAPABILITIES))
    assert "unknown_capability" not in _codes([_route("x", capabilities=(cap,))])


def test_malformed_handler_reference_rejected() -> None:
    # a lazy reference must be a resolvable dotted "module:attr" string; a bare token is malformed
    assert "malformed_reference" in _codes([_route("x", handler="not-a-dotted-ref")])


def test_malformed_parser_factory_reference_rejected() -> None:
    assert "malformed_reference" in _codes([_route("x", parser_factory="bogus")])


def test_wellformed_reference_validates_clean() -> None:
    # Positive path: a well-formed dotted "module.path:attr" handler AND
    # parser_factory on a live route yields NO malformed_reference finding.
    routes = [
        _route(
            "widget",
            handler="rebar._cli.handlers:widget_cmd",
            parser_factory="rebar._cli.grammars:widget_parser",
        )
    ]
    assert "malformed_reference" not in _codes(routes)


def test_retired_route_is_excluded_from_every_derived_set() -> None:
    # A retired spelling is unrouted, so it must contribute to NO derived set -
    # not the group-derived sets and not the flag-derived overlays
    # (_INTERCEPTS, _HIDDEN_ALIASES, _NO_AUTO_MOUNT, _LEGACY_OUTPUT,
    # _CONFIRM_SCOPE). A retired route carrying those flags must still vanish.
    retired = _route(
        "zzz-retired",
        group="intercept",
        retired=True,
        intercept=True,
        no_auto_mount=True,
        legacy_output=True,
        confirmable=True,
    )
    derived = _registry.derive_policy_sets((*_registry.ROUTES, retired))
    for set_name, members in derived.items():
        assert "zzz-retired" not in members, set_name


def test_retired_route_with_handler_is_contradiction() -> None:
    # retired spellings stay unknown/unrouted; carrying a handler contradicts that
    assert "contradiction" in _codes([_route("purge-bridge", retired=True, handler="m:f")])


def test_hidden_and_retired_on_same_route_is_contradiction() -> None:
    # hidden = resolvable-but-undiscoverable; retired = unknown. Mutually exclusive.
    assert "contradiction" in _codes([_route("x", hidden=True, retired=True)])


def test_findings_are_deterministic_in_order() -> None:
    routes = [_route("show"), _route("show"), _route("x", capabilities=("nope",))]
    assert [f.code for f in validate(routes)] == [f.code for f in validate(routes)]


def test_construction_imports_no_command_handlers() -> None:
    # Building routes + looking one up must pull in no heavy OPTIONAL dependency:
    # the registry stores handlers as lazy string references, so importing it never
    # forces the llm/reconciler stacks. Proven in a FRESH child interpreter so the
    # check can never mutate this process's ``sys.modules`` (popping e.g.
    # ``pydantic_ai`` here would poison later tests sharing the xdist worker — mirror
    # of test_audit_serve's subprocess pattern).
    #
    # NOTE: after the RP-05 S6 cutover the router imports NO command-handler module at
    # package-``__init__`` time — the intercept ladder that eagerly imported
    # ``rebar._cli._llm_commands`` / ``_workflow_commands`` was retired, so those advanced
    # command modules must stay OUT of ``sys.modules`` when only the registry is imported.
    # This pins the router's laziness alongside the heavy optional stacks staying out.
    forbidden = (
        "rebar._cli._llm_commands",
        "rebar._cli._workflow_commands",
        "rebar._commands.transition",
        "rebar._commands.delete",
        "rebar._cli._bridge_commands",
        "rebar_reconciler",
        "pydantic_ai",
    )
    code = (
        "import sys, rebar._cli._registry as r;"
        "r.route_for('transition'); r.derive_policy_sets();"
        f"leaked=[m for m in {forbidden!r} if m in sys.modules];"
        "print('LEAK:'+','.join(leaked) if leaked else 'CLEAN')"
    )
    cp = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "CLEAN", f"registry eagerly imported: {cp.stdout.strip()}"


def test_route_lookup_returns_lazy_reference_not_resolved_module() -> None:
    route = _registry.route_for("transition")
    assert route is not None
    # the handler reference is a string (lazy), not an imported callable/module
    assert route.handler is None or isinstance(route.handler, str)
    assert route.parser_factory is None or isinstance(route.parser_factory, str)


def test_unknown_spelling_lookup_returns_none() -> None:
    assert _registry.route_for("definitely-not-a-command") is None


# --- Alias resolution (RP-05 S2a follow-up: route_for is the authoritative resolver) ---
def test_route_for_resolves_an_alias_to_its_owning_route() -> None:
    # An invocation typed as an alias resolves to the Route that declares it.
    routes = (_route("list", aliases=("ls",)),)
    resolved = _registry.route_for("ls", routes=routes)
    assert resolved is not None
    assert resolved.name == "list"


def test_route_for_canonical_name_takes_precedence_over_alias() -> None:
    # When a spelling is BOTH a canonical name (of one route) and an alias (of
    # another), the canonical owner wins — names take precedence over aliases.
    routes = (_route("show"), _route("list", aliases=("show",)))
    resolved = _registry.route_for("show", routes=routes)
    assert resolved is not None
    assert resolved.name == "show"


def test_route_for_does_not_resolve_a_retired_routes_alias() -> None:
    # A retired route is unrouted, so its aliases must not resolve.
    routes = (_route("purge-bridge", retired=True, group="bridge", aliases=("purge",)),)
    assert _registry.route_for("purge", routes=routes) is None


def test_route_for_unknown_spelling_with_explicit_table_returns_none() -> None:
    routes = (_route("list", aliases=("ls",)),)
    assert _registry.route_for("nope", routes=routes) is None


def test_route_for_shipped_table_has_no_alias_regression() -> None:
    # The shipped table declares no aliases today; canonical resolution and the
    # unknown-spelling contract must be unchanged.
    assert _registry.route_for("show") is not None
    assert _registry.route_for("definitely-not-a-command") is None
