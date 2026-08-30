"""Side-effect-free legacy-vs-typed shadow comparator (ADR 0107, e9d5).

Given the SAME ``(direction, action, target, payload, provenance)`` tuple,
builds two ``Mutation`` twins — one with the payload left as a legacy
``dict`` (today's production shape) and one with the payload converted
through :func:`mutation_payloads.build_typed_payload` — and asserts
``mutation.serialize_manifest`` produces byte-identical JSON/hash for both.

This module performs **no I/O**: no Jira transport, no ticket-store write, no
subprocess, no clock sleep, no network. It only constructs ``Mutation``
objects (pure dataclasses) and calls ``serialize_manifest`` (pure — the
docstring on that function already says so). It is exercised by the portable
replay corpus (``tests/fixtures/reconciler/payload_corpus/``) and asserted
side-effect-free by the effect-spy tests
(``tests/unit/rebar_reconciler/mutate/test_payload_shadow_effect_spies.py``).

Callers pass in an already-loaded ``mutation`` module (``mutation_mod``)
rather than importing ``rebar_reconciler.mutation`` here, mirroring the rest
of the package's dynamic by-path-loader convention (ADR 0083): the reconciler
package loads ``mutation.py`` under one canonical ``sys.modules`` key so
``Mutation``/``MutationDirection``/``MutationAction`` keep one class identity
across every caller, and a second, ordinary import here would risk a second,
distinct class identity.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from rebar_reconciler import mutation_payloads


class RejectedByTypedContract(Exception):
    """Raised (by callers, not this module) when a scenario is intentionally
    malformed and the typed contract is expected to reject it. This module's
    ``build_typed_mutation`` simply lets the underlying ``ValueError``/
    ``TypeError``/``UnknownMutationKindError`` propagate — this exception
    exists only as a documented alias tests may catch generically."""


@dataclass(frozen=True)
class ShadowComparisonResult:
    """Outcome of comparing a legacy-payload trace against its typed twin."""

    matched: bool
    legacy_json: str
    legacy_hash: str
    typed_json: str
    typed_hash: str

    @property
    def diff_summary(self) -> str | None:
        if self.matched:
            return None
        return (
            f"legacy_hash={self.legacy_hash} != typed_hash={self.typed_hash}\n"
            f"--- legacy ---\n{self.legacy_json}\n--- typed ---\n{self.typed_json}"
        )


def build_legacy_mutation(
    mutation_mod: Any,
    *,
    direction: str,
    action: str,
    target: str,
    payload: Mapping[str, Any],
    provenance: Mapping[str, Any],
):
    """Construct a ``Mutation`` with the payload left as a plain dict — the
    shape every production producer emits today."""
    return mutation_mod.Mutation(
        direction=mutation_mod.MutationDirection(direction),
        action=mutation_mod.MutationAction(action),
        target=target,
        payload=dict(payload),
        provenance=dict(provenance),
    )


def build_typed_mutation(
    mutation_mod: Any,
    *,
    direction: str,
    action: str,
    target: str,
    payload: Mapping[str, Any],
    provenance: Mapping[str, Any],
):
    """Construct a ``Mutation`` whose payload is the named typed dataclass for
    ``(direction, action)``, converted from the same legacy dict.

    Propagates whatever :func:`mutation_payloads.build_typed_payload` raises
    (``UnknownMutationKindError``/``ValueError``/``TypeError``) for a
    malformed or dead-by-design combination — callers exercising a
    deliberately-invalid corpus scenario should expect (and catch) that.
    """
    typed_payload = mutation_payloads.build_typed_payload(direction, action, payload)
    return mutation_mod.Mutation(
        direction=mutation_mod.MutationDirection(direction),
        action=mutation_mod.MutationAction(action),
        target=target,
        payload=typed_payload,
        provenance=dict(provenance),
    )


def compare_scenario(mutation_mod: Any, scenario: Mapping[str, Any]) -> ShadowComparisonResult:
    """Build the legacy and typed twins for one corpus scenario and compare
    their ``serialize_manifest`` bytes.

    ``scenario`` carries ``direction``/``action``/``target``/``payload``/
    ``provenance`` (see corpus README for the full schema). Raises whatever
    the typed construction raises for a scenario whose ``expect`` is
    ``"reject"`` — callers drive those through ``pytest.raises`` instead of
    calling this function.
    """
    legacy_mut = build_legacy_mutation(
        mutation_mod,
        direction=scenario["direction"],
        action=scenario["action"],
        target=scenario["target"],
        payload=scenario["payload"],
        provenance=scenario.get("provenance", {}),
    )
    typed_mut = build_typed_mutation(
        mutation_mod,
        direction=scenario["direction"],
        action=scenario["action"],
        target=scenario["target"],
        payload=scenario["payload"],
        provenance=scenario.get("provenance", {}),
    )
    legacy_json, legacy_hash = mutation_mod.serialize_manifest([legacy_mut])
    typed_json, typed_hash = mutation_mod.serialize_manifest([typed_mut])
    return ShadowComparisonResult(
        matched=(legacy_hash == typed_hash and legacy_json == typed_json),
        legacy_json=legacy_json,
        legacy_hash=legacy_hash,
        typed_json=typed_json,
        typed_hash=typed_hash,
    )


def compare_corpus(
    mutation_mod: Any, scenarios: list[Mapping[str, Any]]
) -> dict[str, ShadowComparisonResult]:
    """Compare every ``expect: "match"`` scenario in ``scenarios``.

    Scenarios whose ``expect`` is ``"reject"`` are skipped here — they are
    exercised separately (the typed contract is EXPECTED to raise for them).
    Returns ``{scenario_id: ShadowComparisonResult}``.
    """
    results: dict[str, ShadowComparisonResult] = {}
    for scenario in scenarios:
        if scenario.get("expect", "match") != "match":
            continue
        results[scenario["id"]] = compare_scenario(mutation_mod, scenario)
    return results
