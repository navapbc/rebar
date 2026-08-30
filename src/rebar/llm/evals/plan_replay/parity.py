"""Model-parity guard for the plan-review eval harness (ticket magnesian-subjective-ocelot).

Pins each eval pass to the SAME production model class rebar itself resolves for that
pass, on a homogeneous, fallback-free Bedrock chain — evaluating a change on a different
model than production runs confounds the measurement, and ``PydanticAIRunner.run``
re-derives its fallback chain from the resolved model string and ``cfg.repo_path`` on
every call (``runner.py``), so a caller cannot suppress it by simply not building one.
This module hands eval callers an EPHEMERAL config root (an empty-fallback
``[llm.model_classes]`` table) to point their own ``LLMConfig`` at instead.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path
from typing import NamedTuple

from rebar.llm.errors import LLMConfigError
from rebar.llm.model_classes import load_class_slots, resolve_class

PASS_MODEL_CLASS: dict[str, str] = {"pass1": "frontier", "pass2": "standard"}


class PinnedModel(NamedTuple):
    model_id: str
    config_root: str


def resolve_pinned_model(pass_name: str, *, repo_root: str | None = None) -> PinnedModel:
    if pass_name not in PASS_MODEL_CLASS:
        raise ValueError(
            f"unknown eval pass: {pass_name!r} is not one of {sorted(PASS_MODEL_CLASS)}"
        )

    class_name = PASS_MODEL_CLASS[pass_name]
    slots = load_class_slots(repo_root=repo_root)
    resolved = resolve_class(class_name, slots)
    if not resolved.startswith("bedrock:"):
        raise LLMConfigError(
            f"production {class_name!r} class does not resolve to a Bedrock model: {resolved!r}"
        )

    config_root = tempfile.mkdtemp()
    atexit.register(shutil.rmtree, config_root, ignore_errors=True)
    (Path(config_root) / "rebar.toml").write_text(
        f'[llm.model_classes]\n{class_name} = {{ model = "{resolved}", fallback = [] }}\n'
    )
    return PinnedModel(model_id=resolved, config_root=config_root)


def check_model_parity(
    pass_name: str,
    resolved_id: str,
    baseline_ran_model: str,
    *,
    allow_model_change: bool = False,
) -> dict:
    model_change = resolved_id != baseline_ran_model
    if model_change and not allow_model_change:
        raise LLMConfigError(
            f"model parity violated for {pass_name!r}: resolved {resolved_id!r} != "
            f"baseline {baseline_ran_model!r}"
        )
    return {
        "model_change": model_change,
        "resolved_id": resolved_id,
        "baseline_ran_model": baseline_ran_model,
    }


def stamp_pass_model(result: dict, pass_name: str, model_id: str) -> dict:
    stamped = dict(result)
    models = dict(stamped.get("models", {}))
    models[pass_name] = model_id
    stamped["models"] = models
    return stamped


def refuse_diff_on_model_mismatch(result_a: dict, result_b: dict) -> None:
    if "models" not in result_a or "models" not in result_b:
        raise ValueError("cannot diff results: at least one carries no 'models' key")
    models_a = result_a["models"]
    models_b = result_b["models"]
    if set(models_a) != set(models_b):
        raise ValueError(
            f"cannot diff results: pass sets differ ({sorted(models_a)} != {sorted(models_b)})"
        )
    for pass_name in models_a:
        if models_a[pass_name] != models_b[pass_name]:
            raise ValueError(
                f"model mismatch for {pass_name!r}: {models_a[pass_name]!r} != "
                f"{models_b[pass_name]!r}"
            )


def check_cache_effective(usage_rows: list[dict]) -> dict:
    for i, row in enumerate(usage_rows):
        if i >= 1 and row.get("cache_read_tokens", 0) > 0:
            return {"cached": True, "first_cache_hit_row": i}
    return {"cached": False, "first_cache_hit_row": None}
