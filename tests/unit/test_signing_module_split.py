"""Structural contract for the ``rebar.signing`` three-module split (story f5c1-e41d).

``signing.py`` was split along its existing call-graph seams into three concern-scoped
siblings — the manifest vocabulary + gate provenance leaf (``_signing_manifest``), the
legacy symmetric HMAC scheme (``_signing_hmac``), and the entry module that dispatches and
persists (``signing``). Three properties of that split are load-bearing and none of them
is checked by the behavioural suite:

1. **The re-export surface.** ``rebar.signing`` stays the single import point. Every moved
   symbol — public AND private — must remain reachable there as the SAME object.
2. **The import direction.** ``signing -> _signing_hmac -> _signing_manifest``, acyclic.
   A back-edge would reintroduce the import cycle the split exists to avoid.
3. **The monkeypatch seam.** A moved symbol must be patched in the module that DEFINES
   it, because its in-family consumers resolve it as a bare global there. The guards below
   assert POSITIVELY that such a patch is observed — the vacuous-pass class that made this
   split risky is a test that keeps passing while its patch has quietly become a no-op.

The size assertion keeps the family from drifting back to the hard cap: this story exists
because ``signing.py`` reached 800/800 and no edit could land at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from rebar import _signing_hmac, _signing_manifest, signing

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "rebar"

# Every symbol that moved out of signing.py, keyed by its new defining module. The private
# names are re-exported deliberately: `_gate_commit_sha` is imported by
# `rebar/llm/build_drift.py`, and the rest are reached by tests through `rebar.signing`.
MOVED_TO_MANIFEST = (
    "SigningError",
    "parse_manifest",
    "VERIFIED_AT_SHA_PREFIX",
    "verified_at_sha_step",
    "verified_at_sha_from_manifest",
    "verified_at_sha_subject",
    "REBAR_VERSION_PREFIX",
    "rebar_version_step",
    "rebar_version_from_manifest",
    "_gate_source_dir",
    "_baked_commit_sha",
    "_gate_commit_sha",
    "gate_code_version",
    "head_sha",
)
MOVED_TO_HMAC = (
    "ALGORITHM",
    "PAYLOAD_VERSION",
    "_NO_KEY",
    "signing_key",
    "_generate_key_file",
    "key_fingerprint",
    "_canonical_payload",
    "compute_signature",
    "verify_record",
    "_hmac_opcert_not_certified",
)


@pytest.mark.parametrize("name", MOVED_TO_MANIFEST)
def test_manifest_symbols_reexported_from_signing(name: str) -> None:
    """`rebar.signing.<name>` is the SAME object `_signing_manifest` defines."""
    assert hasattr(signing, name), f"rebar.signing lost the re-export of {name!r}"
    assert getattr(signing, name) is getattr(_signing_manifest, name)


@pytest.mark.parametrize("name", MOVED_TO_HMAC)
def test_hmac_symbols_reexported_from_signing(name: str) -> None:
    """`rebar.signing.<name>` is the SAME object `_signing_hmac` defines."""
    assert hasattr(signing, name), f"rebar.signing lost the re-export of {name!r}"
    assert getattr(signing, name) is getattr(_signing_hmac, name)


@pytest.mark.parametrize("name", MOVED_TO_MANIFEST + MOVED_TO_HMAC)
def test_moved_symbols_are_declared_in_all(name: str) -> None:
    """Every re-export is declared in ``signing.__all__``.

    Without it a linter reads the import as unused and a future auto-fix silently deletes
    the re-export — the exact way this contract would rot."""
    assert name in signing.__all__


def _module_level_imports(module_path: Path) -> set[str]:
    """Every ``rebar.*`` module imported at MODULE level (function-local imports excluded)."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:  # top level only — a lazy import inside a function is fine
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            if node.module == "rebar":
                found.update(f"rebar.{alias.name}" for alias in node.names)
    return found


def test_signing_manifest_is_a_leaf() -> None:
    """`_signing_manifest` imports NO rebar module: it is the bottom of the family."""
    imports = _module_level_imports(SRC_ROOT / "_signing_manifest.py")
    assert not {name for name in imports if name.split(".")[0] == "rebar"}


def test_signing_hmac_does_not_import_signing() -> None:
    """`_signing_hmac` may depend on `_signing_manifest`, never back on `signing`."""
    imports = _module_level_imports(SRC_ROOT / "_signing_hmac.py")
    assert "rebar.signing" not in imports
    assert "rebar._signing_hmac" not in _module_level_imports(SRC_ROOT / "_signing_manifest.py")


def test_baked_commit_sha_patch_is_observed_by_gate_commit_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anti-vacuity guard: `_gate_commit_sha` must resolve `_baked_commit_sha` from the
    module the tests patch.

    `test_attestation_signing.py` patches `_baked_commit_sha` to prove the live-checkout
    SHA takes precedence over the wheel-baked one. Land the two symbols in different
    modules and that patch becomes a no-op — and the precedence test that asserts a
    NEGATIVE ("STALEBAKED" not in the result) keeps passing while testing nothing. This
    asserts the POSITIVE: the patched value comes back out."""
    plain = tmp_path / "nogit"  # not a git checkout -> the baked fallback is the only source
    plain.mkdir()
    monkeypatch.setattr(_signing_manifest, "_baked_commit_sha", lambda: "SENTINELBAKED")
    assert _signing_manifest._gate_commit_sha(source_dir=str(plain)) == "SENTINELBAKED"


def test_manifest_vocabulary_patch_is_observed_by_verify_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same guard for the OTHER bare-global pair the split moved apart.

    `verify_record` and `_hmac_opcert_not_certified` read `verified_at_sha_from_manifest` /
    `rebar_version_from_manifest` as bare globals. Those now resolve in `_signing_hmac`'s
    namespace, so a patch on `rebar.signing` no longer reaches them — patch them in
    `_signing_hmac`. No test relied on the old resolution; this pins the new one so the
    change can never happen again silently."""
    monkeypatch.setattr(_signing_hmac, "verified_at_sha_from_manifest", lambda m: "SENTINELSHA")
    monkeypatch.setattr(_signing_hmac, "rebar_version_from_manifest", lambda m: "SENTINELVER")
    verdict = signing.verify_record({"manifest": ["step"]}, "t1", b"key")
    assert verdict["verified_at_sha"] == "SENTINELSHA"
    assert verdict["rebar_version"] == "SENTINELVER"


@pytest.mark.parametrize("module", ["signing.py", "_signing_manifest.py", "_signing_hmac.py"])
def test_signing_family_keeps_real_headroom(module: str) -> None:
    """The split family stays inside the AGENTS.md 200-500 target band, not merely under
    the 800 cap.

    This story exists because `signing.py` sat at 800/800 — no headroom, so NO edit could
    land at all. Clearing the 800 gate by two lines would recreate that within a week, so
    the band is the thing worth enforcing, and only for the three files this split owns.

    Deliberately a CEILING only. A lower bound would fail a behaviour-preserving refactor
    that legitimately deletes code or comments — shrinking is the direction this repo
    wants, and there is no defect a floor would catch."""
    loc = (SRC_ROOT / module).read_text(encoding="utf-8").count("\n")
    assert loc <= 500, f"src/rebar/{module} is {loc} LOC, above the 500 target band"


@pytest.mark.parametrize("module", ["_signing_manifest.py", "_signing_hmac.py"])
def test_split_created_modules_are_not_stubs(module: str) -> None:
    """The two modules this split CREATED carry a real concern, not a fragment.

    AGENTS.md: "never create files < 100 LOC by splitting". This is the one lower bound
    with a justification — it is about the shape of the split itself, not about file size.
    A sibling that erodes to a stub belongs back in its parent rather than left as an extra
    import hop, and this fails when that happens instead of leaving it to notice."""
    loc = (SRC_ROOT / module).read_text(encoding="utf-8").count("\n")
    assert loc >= 100, (
        f"src/rebar/{module} is {loc} LOC — below the 100-LOC floor for a split-created "
        "module; fold it back into signing.py rather than keeping a stub sibling"
    )
