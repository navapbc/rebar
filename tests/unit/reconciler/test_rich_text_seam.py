"""Pin the rich-text seam's send path, mapper ownership, and ADF boundary.

Cloud sends ``normalize_outbound(fit_outbound(v))``. Fitting measures serialized
ADF before normalization. Description sanitization fits only. Exactly one real
``map_fields_to_remote`` implementation lives in the shared layer. Protocol stubs
and pure delegations do not duplicate its logic. ADF entry points stay within the
allowlisted modules.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from _tree_scan import parsed_python_files

_REC = Path(__file__).resolve().parents[3] / "src" / "rebar" / "reconciler"
_ADAPTERS = _REC / "adapters"


# Cloud send-path parity and the distinct description sanitizer.


@pytest.mark.parametrize(
    "text",
    [
        "a short single line",
        "hard\nwrapped\nprose that the ADF encoder rejoins into one paragraph",
        "x" * 100_000,
    ],
    ids=["short", "soft-wrapped", "over-limit"],
)
def test_cloud_send_path_composition_is_unchanged(text: str) -> None:
    """``normalize_outbound(fit_outbound(v))`` == the pre-story Cloud send path.

    Both sides evaluated live against the pinned ADF module, so a reordering, a
    dropped step, or a reimplementation all break this.
    """
    from rebar.reconciler.adapters.jira import adf
    from rebar.reconciler.adapters.jira.rich_text_codec import AdfCodec

    expected = adf.normalize_description(adf.fit_text_to_adf_limit(text))
    codec = AdfCodec()

    assert codec.normalize_outbound(codec.fit_outbound(text)) == expected


def test_the_sanitizer_applies_the_fit_only_not_the_normalization() -> None:
    """The two Cloud rich-text callers are NOT the same transform.

    ``jira_fields._sanitize_description`` is injected with the fit step alone. If
    the codec collapsed fit+normalize into ``fit_outbound``, the sanitizer would
    start normalizing and Cloud's observable behaviour would change — which J1's
    pinned oracles forbid.
    """
    from rebar.reconciler.adapters.jira import adf
    from rebar.reconciler.adapters.jira.jira_fields import _sanitize_description

    # prose whose normalized form DIFFERS from its raw form (soft wraps rejoined)
    text = "one\ntwo\nthree"
    assert adf.normalize_description(text) != text, "fixture must exercise normalization"

    assert _sanitize_description(text) == adf.fit_text_to_adf_limit(text)
    assert _sanitize_description(text) == text, "the sanitizer must NOT normalize"


def test_cloud_backend_description_mapping_still_normalizes() -> None:
    """The other side of the same coin: the BACKEND send path must still apply
    both steps, so the send value and every description comparison converge."""
    from rebar.reconciler.adapters.jira import adf
    from rebar.reconciler.adapters.jira.backend import JiraBackend

    from .backend_support import FakeTransport

    text = "one\ntwo\nthree"
    out = JiraBackend(transport=FakeTransport()).outbound.map_fields_to_remote(
        {"description": text}
    )

    assert out["description"] == adf.normalize_description(adf.fit_text_to_adf_limit(text))


def test_non_string_description_still_bypasses_the_codec_entirely() -> None:
    """Today's mapper guards with ``isinstance(value, str)``; a non-str must reach
    the wire untouched rather than being coerced by the codec."""
    from rebar.reconciler.adapters.jira.backend import JiraBackend

    from .backend_support import FakeTransport

    sentinel = {"already": "adf"}
    out = JiraBackend(transport=FakeTransport()).outbound.map_fields_to_remote(
        {"description": sentinel}
    )

    assert out["description"] is sentinel


# Count the real shared mapping implementation.


def _is_protocol_stub(node: ast.FunctionDef) -> bool:
    """A Protocol declaration's body is a docstring and/or a bare ``...``."""
    body = [
        n
        for n in node.body
        if not (
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        )
    ]
    return (
        all(
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and n.value.value is Ellipsis
            for n in body
        )
        and len(body) > 0
    )


def _is_pure_delegation(node: ast.FunctionDef) -> bool:
    """A pure delegation has one return call to ``map_fields_to_remote``.

    An optional docstring does not count as logic. A forwarding call does not
    duplicate the shared mapping rules.
    """
    body = [
        n
        for n in node.body
        if not (
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        )
    ]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False
    call = body[0].value
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "map_fields_to_remote"
    )


def test_exactly_one_real_map_fields_to_remote_and_it_is_shared() -> None:
    """One mapping implementation lives in the shared layer.

    Exclude the neutral Protocol stub and concrete adapter delegations. They
    preserve the port and adapter contracts without copying mapping logic.
    """
    logic: list[str] = []
    stubs: list[str] = []
    delegations: list[str] = []
    for module in parsed_python_files(_REC):
        if "__pycache__" in module.path.parts:
            continue
        for node in ast.walk(module.tree):
            if isinstance(node, ast.FunctionDef) and node.name == "map_fields_to_remote":
                rel = str(module.path.relative_to(_REC))
                if _is_protocol_stub(node):
                    stubs.append(rel)
                elif _is_pure_delegation(node):
                    delegations.append(rel)
                else:
                    logic.append(rel)

    assert len(logic) == 1, (
        f"expected exactly ONE map_fields_to_remote carrying the mapping logic, found {logic}"
    )
    assert logic[0].startswith("adapters/jira_family/"), (
        f"the single implementation must live in the shared layer, found at {logic[0]}"
    )
    assert any(s.startswith("_backend.py") for s in stubs), (
        "the neutral OutboundMapper Protocol declaration must REMAIN — it is the port contract"
    )
    assert "adapters/jira/backend.py" in delegations + stubs, (
        "the Cloud adapter must expose the shared mapper by delegation (or not define "
        "the method at all) — it must not keep a second copy of the mapping rules"
    )


# Keep ADF entry points within the allowlist.

# Files legitimately allowed to name the ADF entry points, each with its reason.
_ADF_ALLOWLIST = {
    "adapters/jira/adf.py": "the definitions themselves",
    "adapters/jira/rich_text_codec.py": "AdfCodec — the sanctioned Cloud wrapper",
    "inbound_fields.py": "the inbound seam, explicitly out of scope",
    "inbound_differ.py": "the inbound seam, explicitly out of scope",
    "inbound_translate.py": "historical comment only, out of scope",
}


def test_adf_entry_points_are_referenced_only_from_allowlisted_modules() -> None:
    offenders: list[str] = []
    for module in parsed_python_files(_REC):
        if "__pycache__" in module.path.parts:
            continue
        rel = str(module.path.relative_to(_REC))
        if rel in _ADF_ALLOWLIST:
            continue
        text = module.source
        if "fit_text_to_adf_limit" in text or "_load_adf" in text:
            offenders.append(rel)
    assert not offenders, (
        "these modules reach for the ADF encoder directly instead of going through "
        f"the RichTextCodec: {offenders}"
    )


def test_the_shared_layer_names_no_adf_entry_point_at_all() -> None:
    """The sharpest form: zero ADF references anywhere under jira_family/ or
    jira_datacenter/. AdfCodec lives on the CLOUD side precisely so this holds —
    putting it in the shared layer would import the pinned adf.py and break the
    import contract J2 landed."""
    for package in ("jira_family", "jira_datacenter"):
        root = _ADAPTERS / package
        if not root.is_dir():
            continue
        for module in parsed_python_files(root):
            text = module.source
            for name in ("fit_text_to_adf_limit", "_load_adf", "text_to_adf", "adf_to_text"):
                assert name not in text, (
                    f"{package}/{module.path.name} names the Cloud ADF entry point {name!r} — "
                    f"the shared layer must receive rich-text behaviour as a codec"
                )
