#!/usr/bin/env python3
"""Pre-cutover orchestrator: runs capability_check, forward_compat_probe, cursor_snapshot."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _load_step(name: str):
    """Load a step module from the same directory as this script."""
    here = Path(__file__).parent
    step_path = here / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, step_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load step module {name!r} from {step_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return mod


@dataclass
class StepResult:
    name: str
    ok: bool
    message: str
    # Step modules emit structured details (sub-operations, head SHA, in/outbound
    # counts) as a dict — capability_check.StepResult uses a dict and the other
    # step modules do too; the orchestrator's StepResult must agree.
    details: dict = field(default_factory=dict)


def main() -> int:
    steps = ["capability_check", "forward_compat_probe", "cursor_snapshot"]
    passed = 0
    for step_name in steps:
        try:
            mod = _load_step(step_name)
            result: StepResult = mod.run()
        except Exception as exc:
            print(f"FAIL: {step_name} — exception: {exc!r}", file=sys.stderr)
            return 1
        if not result.ok:
            print(f"FAIL: {result.name} — {result.message}", file=sys.stderr)
            return 1
        # cursor_snapshot exposes `details['committed']` to distinguish
        # "snapshot committed to tickets branch" from "snapshot written but
        # commit skipped because we're not on the tickets branch". The step
        # itself still counts as passed (the snapshot file IS durable on
        # disk), but operators need a visible signal that no commit landed
        # on the orphan branch — otherwise downstream cutover may proceed
        # on a stale tickets-branch view.
        if step_name == "cursor_snapshot" and result.details.get("committed") is False:
            print(
                f"WARN: {result.name} — commit step was skipped "
                f"(branch={result.details.get('branch')!r}); "
                f"snapshot written locally but NOT committed to the tickets "
                f"orphan branch. Downstream consumers that read from the "
                f"tickets branch will see stale state until the commit is "
                f"manually routed.",
                file=sys.stderr,
            )
        passed += 1
    print(f"OK: pre_cutover ({passed}/{len(steps)} steps passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
