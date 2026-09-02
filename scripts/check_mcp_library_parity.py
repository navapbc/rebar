#!/usr/bin/env python3
"""Drift gate: the MCP tool surface vs the ``rebar`` library facade.

WHY THIS EXISTS
---------------
rebar ships ONE store behind three surfaces — the library (``import rebar``), the CLI, and
the MCP server. The MCP tools are thin closures over the library facade (``rebar.__all__``),
but NOTHING cross-checks the two: a library function can gain a parameter, lose one, or be
renamed while its MCP tool keeps the old shape, and every existing gate stays green. The MCP
reference generator (``scripts/gen_mcp_reference.py``) only documents what the registrars
expose; it never looks at the library at all. So the two surfaces can drift silently, and an
agent driving rebar over MCP gets a capability the library grew months ago — or does not.

This gate pins the correspondence in a COMMITTED manifest
(``tests/unit/mcp_library_parity_manifest.json``) and fails the build when either side moves
away from it. Every tool is classified by how it reaches the facade:

    exact     the tool name IS a facade symbol (``show_ticket`` -> ``rebar.show_ticket``)
    co_names  the closure calls a facade symbol under a different tool name
              (``claim_ticket`` -> ``rebar.claim``)
    mcp_only  no library counterpart at all — legitimately MCP-shaped (``gate_status``)

NORMALIZATION (why a raw parameter diff would be useless)
--------------------------------------------------------
Every exact-match tool omits the library's ``repo_root`` parameter, because the MCP server
resolves the store root from its own environment rather than from the caller. That is a
CATEGORY-LEVEL rule, not per-tool drift: diffing raw parameter sets would flag all 35
exact-match tools and the gate would be noise. The rule is therefore recorded ONCE, under
the manifest's ``normalization`` key, and applied during comparison.

Parameter parity is required only for ``exact`` tools — a shared name is a claim that the two
surfaces are the same operation. ``co_names`` correspondences are heuristic (a differently
named tool wrapping a facade call), and ``mcp_only`` tools have nothing to compare against, so
neither is held to a signature contract; the manifest still records their shape so a change
there is visible in review.

DECLARED DIVERGENCE
-------------------
An exact-match tool whose parameters legitimately differ carries a ``divergence`` block with a
``kind`` and a NON-EMPTY ``reason``. An empty or absent reason is an UNDECLARED divergence and
fails — that is the whole point: the gate accepts justified difference, never silent
difference.

The manifest is also checked for coherence ON ITS OWN TERMS, because a human hand-edits it to
write those reasons and a bad edit must fail loudly rather than quietly disabling a check:

* a ``divergence`` block ALWAYS needs a non-empty ``reason``, on ANY entry — not only on the
  ``exact`` entries whose parameters are compared, and not only when the surfaces differ;
* ``correspondence`` and ``library_symbol`` must agree — ``exact``/``co_names`` name a facade
  counterpart so the symbol cannot be null, and ``mcp_only`` asserts there is none so the
  symbol must be null.

To fix a failure: run ``python scripts/check_mcp_library_parity.py --update`` from the repo
root to rewrite the manifest from the live surfaces, then review the diff. ``--update``
carries existing ``divergence`` declarations forward and writes an EMPTY-reason stub for any
newly-diverging tool, so a fresh divergence still fails ``--check`` until a human writes the
justification into the manifest.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = "tests/unit/mcp_library_parity_manifest.json"

SCHEMA_VERSION = 1

# The category-level rule: parameters the LIBRARY takes that no MCP tool ever exposes,
# because the server resolves them from its own environment.
LIBRARY_ONLY_PARAMS = ["repo_root"]

# The five fields that make up a normalized descriptor. ``divergence`` is deliberately NOT
# here: it is a manifest-only DECLARATION about a descriptor, never part of the live surface.
DESCRIPTOR_FIELDS = (
    "registrar",
    "library_symbol",
    "correspondence",
    "mcp_params",
    "library_params",
)

CORRESPONDENCES = frozenset({"exact", "co_names", "mcp_only"})

FIX_HINT = "python scripts/check_mcp_library_parity.py --update"


# ── Live enumeration ────────────────────────────────────────────────────────────────────


def _registrar_tools() -> dict[str, dict[str, Any]]:
    """Enumerate each MCP registrar onto its OWN ``FastMCP`` and return
    ``{"read"|"llm"|"write": {tool_name: tool_object}}``.

    Mirrors ``scripts/gen_mcp_reference.py::_registrar_tools``: the gate env vars are set
    BEFORE importing the server so the write registrar does not return early and the
    LLM/Jira gates are unambiguous (they only fire at *call* time, never at registration).

    ``mcp`` is a real dependency of this repo, so an import failure here is a broken
    environment and MUST surface — it is never swallowed into a silently-skipped gate.
    """
    os.environ["REBAR_MCP_READONLY"] = "0"
    os.environ["REBAR_MCP_ALLOW_LLM"] = "1"
    os.environ["REBAR_MCP_ALLOW_JIRA_SYNC"] = "1"

    from mcp.server.fastmcp import FastMCP

    import rebar.mcp_server as ms
    from rebar._mcp_llm import register_llm_tools
    from rebar._mcp_reads import register_read_tools
    from rebar._mcp_writes import register_write_tools

    def _group(reg: Any) -> dict[str, Any]:
        m = FastMCP("x")
        ctx = SimpleNamespace(
            readonly=ms._readonly,
            allow_llm=ms._allow_llm,
            allow_jira_sync=ms._allow_jira_sync,
            cap_workflow_payload=ms._cap_workflow_payload,
            bound_list_payload=ms._bound_list_payload,
            dump=ms._dump,
            MODE_CAPS=ms.MODE_CAPS,
            Mode=ms.Mode,
            logger=ms.logger,
        )
        reg(m, ctx)
        return dict(m._tool_manager._tools)  # private handle: the registered-tool map

    return {
        "read": _group(register_read_tools),
        "llm": _group(register_llm_tools),
        "write": _group(register_write_tools),
    }


def _mcp_params(tool: Any) -> list[str]:
    """The tool's declared input parameters, from its generated JSON schema."""
    return sorted((tool.parameters or {}).get("properties") or {})


def _shipped(obj: Any) -> bool:
    """Is this object rebar's own shipped definition (rather than a stand-in)?"""
    module = getattr(obj, "__module__", "") or ""
    return module == "rebar" or module.startswith("rebar.")


def _facade_function(facade: Any, symbol: str) -> Any:
    """The SHIPPED implementation behind a facade name.

    ``rebar.<symbol>`` is a plain module attribute, so a harness can rebind it — the unit
    tier's autouse ``_no_real_session_log_writes`` fixture replaces
    ``rebar.append_session_log`` with a ``(*_args, **_kwargs)`` stub for the whole tier. A
    gate that read the signature straight off the facade would then report the STUB's
    parameters and fail on a repository that has not drifted at all. So when the bound
    attribute is not one of rebar's own definitions, recover it from the submodule that
    defines it (the defining module is the one whose name matches ``__module__``, which
    keeps the resolution deterministic when a name is re-exported).
    """
    obj = getattr(facade, symbol)
    if _shipped(obj):
        return obj
    for module_name in sorted(sys.modules):
        if not module_name.startswith("rebar."):
            continue
        candidate = getattr(sys.modules[module_name], symbol, None)
        if candidate is not None and getattr(candidate, "__module__", None) == module_name:
            return candidate
    return obj


def _library_params(facade: Any, symbol: str) -> list[str]:
    """The facade function's declared parameters (positional and keyword alike)."""
    return sorted(inspect.signature(_facade_function(facade, symbol)).parameters)


def _classify(name: str, tool: Any, exported: frozenset[str]) -> tuple[str, str | None]:
    """Resolve a tool to its facade symbol -> ``(correspondence, library_symbol)``.

    An exact name match wins. Otherwise the closure's own constant/global names are
    intersected with the facade (``rebar.claim`` inside ``claim_ticket``); the FIRST such
    name in code order is the correspondence, which is deterministic per build.
    """
    if name in exported:
        return "exact", name
    for candidate in tool.fn.__code__.co_names:
        if candidate in exported:
            return "co_names", candidate
    return "mcp_only", None


def build_live_surface() -> dict:
    """Enumerate the live MCP tool registry + library facade into a normalized surface.

    Shape matches the committed manifest exactly (minus ``divergence`` declarations), so
    ``evaluate`` can compare the two structurally.
    """
    import rebar

    # Only callable exports can be a tool's counterpart; a constant in ``__all__`` that
    # happens to appear in a closure's names is not a correspondence.
    exported = frozenset(n for n in rebar.__all__ if callable(getattr(rebar, n, None)))
    tools: dict[str, dict[str, Any]] = {}
    for registrar, group in _registrar_tools().items():
        for name, tool in group.items():
            correspondence, symbol = _classify(name, tool, exported)
            tools[name] = {
                "registrar": registrar,
                "library_symbol": symbol,
                "correspondence": correspondence,
                "mcp_params": _mcp_params(tool),
                "library_params": _library_params(rebar, symbol) if symbol else [],
            }
    return {
        "schema_version": SCHEMA_VERSION,
        "normalization": {"library_only_params": list(LIBRARY_ONLY_PARAMS)},
        "tools": tools,
    }


# ── Manifest I/O ────────────────────────────────────────────────────────────────────────


def render_manifest(surface: dict) -> str:
    """Canonical JSON text for the committed manifest.

    Sorted keys + 2-space indent + trailing newline, so a regeneration produces a
    reviewable diff rather than a reordering.
    """
    return json.dumps(surface, indent=2, sort_keys=True) + "\n"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"{MANIFEST_PATH}: {message}")


def parse_manifest(raw: str) -> dict:
    """Parse + structurally validate the committed manifest."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{MANIFEST_PATH}: not valid JSON ({exc})") from exc
    _require(isinstance(data, dict), "top level is not an object")
    _require(isinstance(data.get("normalization"), dict), "missing/invalid `normalization`")
    tools = data.get("tools")
    _require(isinstance(tools, dict), "missing/invalid `tools`")
    for name, entry in tools.items():
        _require(isinstance(entry, dict), f"tool `{name}` is not an object")
        for field in DESCRIPTOR_FIELDS:
            _require(field in entry, f"tool `{name}` is missing `{field}`")
        _require(
            entry["correspondence"] in CORRESPONDENCES,
            f"tool `{name}` has unknown correspondence `{entry['correspondence']}`",
        )
    return data


def _normalization(surface: dict) -> list[str]:
    return list((surface.get("normalization") or {}).get("library_only_params") or [])


def _descriptor(entry: dict) -> dict:
    return {field: entry.get(field) for field in DESCRIPTOR_FIELDS}


# ── Pure evaluation ─────────────────────────────────────────────────────────────────────

#: Correspondences that name a facade symbol, and so require a non-null ``library_symbol``.
_SYMBOL_REQUIRED = frozenset({"exact", "co_names"})


def _validate_symbol_pairing(name: str, entry: dict) -> list[str]:
    """``correspondence`` and ``library_symbol`` must agree with each other.

    ``exact``/``co_names`` both ASSERT a facade counterpart, so a null symbol is incoherent;
    ``mcp_only`` asserts there is none, so a symbol is equally incoherent. The manifest is
    hand-editable (a human writes divergence reasons into it), and either mismatch silently
    disables a check rather than tripping one — so it must fail loudly.
    """
    correspondence = entry.get("correspondence")
    symbol = entry.get("library_symbol")
    if correspondence in _SYMBOL_REQUIRED and not symbol:
        return [
            f"  INCOHERENT DESCRIPTOR tool `{name}`: correspondence `{correspondence}` names a "
            f"library counterpart, so `library_symbol` cannot be {symbol!r}"
        ]
    if correspondence == "mcp_only" and symbol is not None:
        return [
            f"  INCOHERENT DESCRIPTOR tool `{name}`: correspondence `mcp_only` asserts there is "
            f"no library counterpart, so `library_symbol` must be null, got {symbol!r}"
        ]
    return []


def _validate_divergence(name: str, entry: dict) -> list[str]:
    """A ``divergence`` block ALWAYS needs a non-empty ``reason``, wherever it appears.

    Checked independently of ``correspondence`` and of whether the surfaces currently
    differ: a declaration with no justification is malformed on ANY entry, exactly as a bare
    ``# read-via:`` marker is an error in ``scripts/check_config_reads.py``. Without this the
    mandatory-reason convention would only hold on the ``exact`` entries whose parameters the
    gate compares, and an empty reason could sit unnoticed on every other tool.
    """
    if "divergence" not in entry:
        return []
    declaration = entry.get("divergence")
    if not isinstance(declaration, dict):
        return [
            f"  MALFORMED DIVERGENCE tool `{name}`: `divergence` must be an object carrying "
            f"`kind` and a non-empty `reason`, got {type(declaration).__name__}"
        ]
    if not _declared_reason(entry):
        return [
            f"  UNJUSTIFIED DIVERGENCE tool `{name}`: its `divergence` block has no `reason` "
            f"(absent, empty, or whitespace only) — a declaration must state WHY the MCP tool "
            f"and the library function differ, or it is indistinguishable from silent drift"
        ]
    return []


def _validate_entries(tools: dict) -> list[str]:
    """Per-descriptor coherence for one surface, independent of the other."""
    messages: list[str] = []
    for name in sorted(tools):
        entry = tools[name]
        if not isinstance(entry, dict):
            messages.append(f"  MALFORMED DESCRIPTOR tool `{name}` is not an object")
            continue
        messages += _validate_symbol_pairing(name, entry)
        messages += _validate_divergence(name, entry)
    return messages


def _validate_manifest(manifest: dict) -> list[str]:
    """Every coherence rule the manifest must satisfy on its own terms."""
    return _validate_entries(manifest.get("tools") or {})


def _check_normalization(live: dict, manifest: dict) -> list[str]:
    """The category-level rules themselves must not drift."""
    messages: list[str] = []
    if live.get("schema_version") != manifest.get("schema_version"):
        messages.append(
            f"  CHANGED schema_version: manifest={manifest.get('schema_version')!r} "
            f"live={live.get('schema_version')!r}"
        )
    if _normalization(live) != _normalization(manifest):
        messages.append(
            f"  CHANGED normalization.library_only_params: "
            f"manifest={_normalization(manifest)} live={_normalization(live)}"
        )
    return messages


def _check_membership(live_tools: dict, manifest_tools: dict) -> list[str]:
    """Tools that appeared or vanished since the manifest was generated."""
    messages: list[str] = []
    for name in sorted(set(live_tools) - set(manifest_tools)):
        messages.append(
            f"  ADDED tool `{name}` ({live_tools[name].get('registrar')} registrar) is live "
            f"but absent from the manifest"
        )
    for name in sorted(set(manifest_tools) - set(live_tools)):
        messages.append(f"  REMOVED tool `{name}` is in the manifest but no longer registered")
    return messages


def _check_descriptors(live_tools: dict, manifest_tools: dict) -> list[str]:
    """Field-level drift for tools present on both sides."""
    messages: list[str] = []
    for name in sorted(set(live_tools) & set(manifest_tools)):
        live_desc = _descriptor(live_tools[name])
        manifest_desc = _descriptor(manifest_tools[name])
        for field in DESCRIPTOR_FIELDS:
            if live_desc[field] != manifest_desc[field]:
                messages.append(
                    f"  CHANGED tool `{name}` field `{field}`: "
                    f"manifest={manifest_desc[field]!r} live={live_desc[field]!r}"
                )
    return messages


def _diverges(entry: dict, library_only: list[str]) -> tuple[list[str], list[str]]:
    """Normalized parameter difference for one descriptor -> (mcp_only, library_only)."""
    mcp = set(entry.get("mcp_params") or [])
    library = set(entry.get("library_params") or []) - set(library_only)
    return sorted(mcp - library), sorted(library - mcp)


def _declared_reason(entry: dict) -> str:
    declaration = entry.get("divergence") or {}
    reason = declaration.get("reason") if isinstance(declaration, dict) else None
    return reason.strip() if isinstance(reason, str) else ""


def _check_divergences(
    live_tools: dict, manifest_tools: dict, library_only: list[str]
) -> list[str]:
    """Every exact-name correspondence must agree on parameters, or say why it does not.

    Runs against the LIVE surface (so a divergence fails even when the manifest and the live
    surface agree with each other) and reads the declaration from the MANIFEST (the only
    place a human can write one).
    """
    messages: list[str] = []
    for name in sorted(live_tools):
        entry = live_tools[name]
        if entry.get("correspondence") != "exact":
            continue
        extra_mcp, extra_library = _diverges(entry, library_only)
        declared = _declared_reason(manifest_tools.get(name, {}))
        if extra_mcp or extra_library:
            if not declared:
                messages.append(
                    f"  UNDECLARED DIVERGENCE tool `{name}` vs `rebar.{entry['library_symbol']}`: "
                    f"MCP-only params={extra_mcp} library-only params={extra_library}"
                )
        elif declared:
            messages.append(
                f"  STALE DIVERGENCE tool `{name}` declares a divergence "
                f"({declared[:60]!r}) but its surfaces now agree — drop the declaration"
            )
    return messages


def evaluate(live: dict, manifest: dict) -> tuple[int, list[str]]:
    """PURE: return ``(0, [confirmation])`` on parity, ``(1, [diagnostics])`` on drift.

    Injectable by design — a seeded mismatch is provable on synthetic descriptors without
    touching the repository or importing the MCP server.
    """
    live_tools = live.get("tools") or {}
    manifest_tools = manifest.get("tools") or {}
    library_only = _normalization(manifest)

    findings: list[str] = []
    # Coherence FIRST: a malformed descriptor makes the comparison below untrustworthy, and
    # both surfaces are validated (the live one by construction, the manifest because a hand
    # edit can break it). Identical messages are reported once.
    for messages in (_validate_manifest(manifest), _validate_entries(live_tools)):
        findings += [message for message in messages if message not in findings]
    findings += _check_normalization(live, manifest)
    findings += _check_membership(live_tools, manifest_tools)
    findings += _check_descriptors(live_tools, manifest_tools)
    findings += _check_divergences(live_tools, manifest_tools, library_only)

    if not findings:
        counts = _correspondence_counts(live_tools)
        return 0, [
            f"MCP/library parity: OK ({len(live_tools)} tools — "
            f"{counts['exact']} exact, {counts['co_names']} co_names, "
            f"{counts['mcp_only']} MCP-only; normalized on {library_only})."
        ]

    header = [
        "::error::MCP tool surface and rebar library facade have DRIFTED from "
        f"{MANIFEST_PATH} — an MCP tool no longer matches the library function it wraps."
    ]
    footer = [
        f"  To fix: regenerate the manifest with `{FIX_HINT}` and review the diff.",
        "  An UNDECLARED DIVERGENCE is not fixed by --update alone: it writes an "
        "empty-reason `divergence` stub, and the gate stays red until you write the "
        "justification (a non-empty `reason`) into the manifest.",
    ]
    return 1, header + findings + footer


def _correspondence_counts(tools: dict) -> dict[str, int]:
    counts = dict.fromkeys(CORRESPONDENCES, 0)
    for entry in tools.values():
        kind = entry.get("correspondence")
        if kind in counts:
            counts[kind] += 1
    return counts


# ── CLI ─────────────────────────────────────────────────────────────────────────────────


def load_committed() -> dict:
    """Read + validate the committed manifest (the gate's I/O half)."""
    path = REPO_ROOT / MANIFEST_PATH
    if not path.exists():
        raise SystemExit(f"{MANIFEST_PATH} is missing — generate it with `{FIX_HINT}`")
    return parse_manifest(path.read_text(encoding="utf-8"))


def merge_declarations(live: dict, previous: dict) -> dict:
    """Carry declared divergences forward onto a freshly-enumerated surface.

    A tool that still diverges keeps its existing declaration. A NEWLY-diverging tool gets an
    empty-reason stub instead of an invented justification, so ``--update`` can never launder
    a real divergence into silence — ``--check`` still fails until a human writes the reason.
    """
    library_only = _normalization(live)
    previous_tools = previous.get("tools") or {}
    for name, entry in (live.get("tools") or {}).items():
        if entry.get("correspondence") != "exact":
            continue
        extra_mcp, extra_library = _diverges(entry, library_only)
        if not (extra_mcp or extra_library):
            continue
        prior = (previous_tools.get(name) or {}).get("divergence")
        entry["divergence"] = (
            dict(prior)
            if isinstance(prior, dict) and _declared_reason({"divergence": prior})
            else {"kind": "undeclared", "reason": ""}
        )
    return live


def _update(live: dict) -> int:
    path = REPO_ROOT / MANIFEST_PATH
    previous: dict = {}
    if path.exists():
        try:
            previous = parse_manifest(path.read_text(encoding="utf-8"))
        except ValueError:
            previous = {}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_manifest(merge_declarations(live, previous)), encoding="utf-8")
    print(f"Wrote {MANIFEST_PATH} ({len(live.get('tools') or {})} tools).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check",
        action="store_true",
        help="fail when the live surfaces have drifted from the manifest (the default)",
    )
    group.add_argument(
        "--update", action="store_true", help="rewrite the manifest from the live surfaces"
    )
    args = parser.parse_args(argv)

    live = build_live_surface()
    if args.update:
        return _update(live)

    code, messages = evaluate(live, load_committed())
    for message in messages:
        print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
