#!/usr/bin/env python3
"""Pass-planning policy for apply(): mode caps, suppression, manifest emission.

The "what to apply this pass, and how to report it" layer that apply()'s
orchestrator skeleton delegates to:
  * _load_mode_module / _load_manifest_renderer — lazy loaders for the mode-cap
    and manifest-rendering siblings,
  * _mode_sort_key — the deterministic cap-ordering key,
  * _partition_by_mode_cap — coerce the mode + split mutations into applied/deferred,
  * _SuppressionIndex — the suppress-pair O(1) index the inbound loop maintains,
  * _emit_mode_manifest — render + atomically write the mode-specific manifest,
    returning an (action, value) sentinel so the apply() shell performs its own
    early returns.

Imports downward only (inbound_translate for the local-id form); never applier.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

from rebar_reconciler.inbound_translate import _jira_key_to_local_id


def _load_mode_module():
    """Lazy-load mode.py under a stable key so MODE_CAPS / Mode are accessible.

    Uses the SAME dotted key as __main__._MODE_KEY so a single module object
    is shared with the entry-point loader; tests that pre-seed sys.modules
    under that key see their stub here too.
    """
    key = "rebar_reconciler.mode"
    if key in sys.modules:
        return sys.modules[key]
    mode_path = Path(__file__).parent / "mode.py"
    spec = importlib.util.spec_from_file_location(key, mode_path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"mode.py not found at {mode_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_manifest_renderer():
    """Lazy-load manifest_renderer.py."""
    key = "rebar_reconciler.manifest_renderer"
    if key in sys.modules:
        return sys.modules[key]
    path = Path(__file__).parent / "manifest_renderer.py"
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"manifest_renderer.py not found at {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _mode_sort_key(m) -> tuple[str, str, str]:
    """Deterministic ordering key for cap enforcement.

    Outbound creates sort first (priority "0") so they land within the
    bootstrap-strict cap window. Without this, 'inbound' < 'outbound'
    lexicographically causes all cap slots to go to inbound mutations,
    deferring outbound creates indefinitely (bug d5a2-3fc8).
    """
    d = getattr(m, "direction", None)
    a = getattr(m, "action", None)
    t = getattr(m, "target", None)
    if isinstance(m, dict):
        d = d if d is not None else m.get("direction", "")
        a = a if a is not None else m.get("action", "")
        t = t if t is not None else (m.get("key", "") or m.get("target", ""))
    d_str = str(getattr(d, "value", d) or "")
    a_str = str(getattr(a, "value", a) or "")
    if d_str == "outbound" and a_str == "create":
        d_str = "0_outbound_create"
    return (d_str, a_str, str(t or ""))


def _partition_by_mode_cap(mode, mutations):
    """Coerce *mode* to a Mode enum and partition *mutations* per its cap.

    Returns ``(mode, mode_mod, mutations_input, deferred_for_manifest)``. When
    *mode* is None the mutations pass through unchanged (legacy contract);
    otherwise the list is sorted by _mode_sort_key, the first ``cap`` applied and
    the remainder deferred (cap None = uncapped LIVE, cap 0 = DRY_RUN defers all).
    """
    mutations_input = list(mutations or [])
    deferred_for_manifest: list = []
    # Hoist the mode module load to a single call per apply() invocation.
    mode_mod = _load_mode_module() if mode is not None else None
    if mode is not None:
        # Validate / coerce mode to a Mode enum member (findings #1/#2).
        if isinstance(mode, str):
            mode = mode_mod.Mode.from_str(mode)
        if not isinstance(mode, mode_mod.Mode):
            raise TypeError(
                f"mode must be a Mode enum member or a recognised mode string, "
                f"got {type(mode).__name__}: {mode!r}"
            )
        cap = mode_mod.MODE_CAPS.get(mode)
        # Sort deterministically before applying the cap so the applied /
        # deferred partition is reproducible across passes.
        ordered = sorted(mutations_input, key=_mode_sort_key)
        if cap is None:
            mutations_input = ordered
        elif cap == 0:
            deferred_for_manifest = ordered
            mutations_input = []
        else:
            mutations_input = ordered[:cap]
            deferred_for_manifest = ordered[cap:]
    return mode, mode_mod, mutations_input, deferred_for_manifest


class _SuppressionIndex:
    """O(1) suppress-pair index for the inbound dispatch loop.

    Maintains two sets of canonical identifiers (jira-keys-as-given and local_ids)
    plus a set of computed local-id forms (jira_key -> _jira_key_to_local_id) so the
    computed-form match arm (target=='DIG-7' suppresses subsequent target=='jira-dig-7')
    is also O(1). Replaces the prior per-apply closures over module-level sets.
    """

    __slots__ = ("suppressed_targets", "suppressed_pairs")

    def __init__(self) -> None:
        self.suppressed_targets: set[str] = set()
        self.suppressed_pairs: set[tuple[str, str]] = set()

    def is_suppressed(self, target: str) -> bool:
        if not target:
            return False
        return target in self.suppressed_targets

    def record(self, local_id: str, jira_key: str) -> None:
        self.suppressed_pairs.add((local_id, jira_key))
        if jira_key:
            self.suppressed_targets.add(jira_key)
            self.suppressed_targets.add(_jira_key_to_local_id(jira_key))
        if local_id:
            self.suppressed_targets.add(local_id)


def _emit_mode_manifest(
    mode,
    mode_mod,
    mutations_list,
    deferred_for_manifest,
    pass_id,
    manifest_path,
    repo_root,
    persist,
):
    """Render + write the mode-specific manifest; return an (action, value) sentinel.

    The apply() shell performs the early returns this layer signals:
      * ("RETURN", None)             — LIVE: legacy manifest removed, no file.
      * ("RETURN", rendered_dict)    — no-persist (cap-0): plan returned, no file.
      * ("PATH", manifest_path)      — manifest written atomically; shell returns it.
    """
    renderer_mod = _load_manifest_renderer()
    applied_for_manifest = list(mutations_list)

    if mode == mode_mod.Mode.LIVE:
        # LIVE: no manifest file per contract. Remove the legacy manifest
        # written by _apply_batch.
        try:
            if manifest_path is not None and Path(manifest_path).exists():
                Path(manifest_path).unlink()
        except OSError:
            pass
        return ("RETURN", None)

    if mode == mode_mod.Mode.BOOTSTRAP_THROTTLE:
        rendered = renderer_mod.render_throttle(applied_for_manifest, deferred_for_manifest)
    else:
        # DRY_RUN and BOOTSTRAP_STRICT share the same renderer.
        rendered = renderer_mod.render_dry_run_or_strict(
            applied_for_manifest, deferred_for_manifest
        )

    rendered_with_meta = {
        "pass_id": pass_id,
        "mode": getattr(mode, "value", str(mode)),
        "applied_count": rendered.get("applied_count", len(applied_for_manifest)),
        "deferred_count": rendered.get("deferred_count", len(deferred_for_manifest)),
        "outbound": rendered.get("outbound"),
        "inbound": rendered.get("inbound"),
    }
    if "spot_check" in rendered:
        rendered_with_meta["spot_check"] = rendered["spot_check"]
    # Also expose the deferred mutations list (sorted) so tests and
    # operators can audit exactly what was held back.
    rendered_with_meta["deferred"] = [
        {
            "direction": str(
                getattr(getattr(m, "direction", ""), "value", "")
                or (m.get("direction", "") if isinstance(m, dict) else "")
            ),
            "action": str(
                getattr(getattr(m, "action", ""), "value", "")
                or (m.get("action", "") if isinstance(m, dict) else "")
            ),
            "target": _mode_sort_key(m)[2],
        }
        for m in deferred_for_manifest
    ]

    # No-write contract (cap-0 modes, persist=False): produce the full
    # computed plan as a dict and RETURN it WITHOUT writing any manifest file.
    if not persist:
        return ("RETURN", rendered_with_meta)

    # DRY_RUN may have skipped _apply_batch entirely (when mutations_input
    # was empty) — _apply_batch still wrote an empty manifest. Either way,
    # the manifest_path is valid; overwrite with the asymmetric shape.
    if manifest_path is None:
        if repo_root is None:
            repo_root_resolved = Path(
                os.environ.get("REBAR_ROOT") or Path(__file__).resolve().parents[4]
            )
        else:
            repo_root_resolved = repo_root
        snapshots_dir = repo_root_resolved / "bridge_state" / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = snapshots_dir / f"{pass_id}.manifest.json"
    # Atomic write via tempfile + os.replace to avoid race conditions
    # when concurrent DRY_RUN passes share the same pass_id (finding #3).
    manifest_dir = Path(manifest_path).parent
    manifest_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=manifest_dir,
        prefix=f"{pass_id}.",
        suffix=".json.tmp",
    )
    try:
        with os.fdopen(fd, "w") as tmp_f:
            json.dump(rendered_with_meta, tmp_f, indent=2)
        os.replace(tmp_path, str(manifest_path))
    except BaseException:
        # Clean up the temp file on any failure.
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    return ("PATH", manifest_path)
