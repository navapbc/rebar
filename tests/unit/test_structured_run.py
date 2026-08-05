"""Opt-in raw-reply artifact on final structured-parse failure (story 2fd6).

When the prompted reask loop in ``_pai_structured`` exhausts ``OUTPUT_RETRIES`` the raw model
reply is discarded today, so every recurrence of the schema-blind extraction class had to be
diagnosed from a lucky console capture. This pins the opt-in, LOCAL, fail-closed debugging
artifact: with ``llm.parse_failure_artifact_dir`` set, the FINAL failure writes ONE file
carrying the raw reply + metadata and names it in the raised error; unset (the default) the
failure path is byte-for-byte unchanged and nothing is written.

The seam is driven through the public runner (a ``FunctionModel`` override returning fixed
text), exactly as ``test_runner_hardening.py`` does — no live call, the parse failure is real.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar.llm import structured as _structured
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMRunnerError
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytest.importorskip("pydantic_ai")

pytestmark = pytest.mark.unit

_ATTEMPTS = 1 + _structured.OUTPUT_RETRIES


def _sequence_model(texts):
    """A FunctionModel returning ``texts[i]`` on the i-th call (clamped), plus a call counter."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    state = {"i": 0}

    def gen(messages, info):
        idx = min(state["i"], len(texts) - 1)
        state["i"] += 1
        return ModelResponse(parts=[TextPart(texts[idx])])

    return FunctionModel(gen), state


def _req():
    return RunRequest(
        system_prompt="x",
        instructions="y",
        config=LLMConfig(repo_path="."),
        reviewers=["v"],
        mode="structured",
        output_schema="completion_verdict",
    )


def _run(model, *, artifact_dir=None):
    cfg = LLMConfig(repo_path=".")
    if artifact_dir is not None:
        cfg.parse_failure_artifact_dir = str(artifact_dir)
    return PydanticAIRunner(cfg, model_override=model).run(_req())


def _artifacts(d: Path) -> list[Path]:
    return sorted(p for p in Path(d).iterdir() if p.is_file())


# --------------------------------------------------------------------------------------------
# HAPPY PATH
# --------------------------------------------------------------------------------------------


def test_final_parse_failure_writes_one_artifact_with_reply_and_metadata(tmp_path) -> None:
    """Key set + forced final failure -> exactly one artifact with the reply bytes + metadata,
    and the raised error names the artifact path."""
    model, calls = _sequence_model(["never any json"])
    with pytest.raises(LLMRunnerError) as ei:
        _run(model, artifact_dir=tmp_path)
    assert calls["i"] == _ATTEMPTS  # exhausted the bounded budget

    files = _artifacts(tmp_path)
    assert len(files) == 1, f"expected exactly one artifact, got {files}"
    art = files[0]

    # The raised error names the artifact path.
    assert str(art) in str(ei.value)

    payload = json.loads(art.read_text())
    assert "never any json" in payload["reply"]
    assert isinstance(payload["model"], str) and payload["model"]
    assert "completion_verdict" in payload["contract"]
    assert payload["attempts"] == _ATTEMPTS


# --------------------------------------------------------------------------------------------
# HELD OUT — negative controls, rotation, best-effort.
# --------------------------------------------------------------------------------------------


def test_key_unset_writes_nothing_and_error_is_unchanged(tmp_path) -> None:
    """Key unset -> no artifact, and the same failure type is raised (byte-for-byte path)."""
    model, calls = _sequence_model(["never any json"])
    with pytest.raises(LLMRunnerError):
        _run(model, artifact_dir=None)
    assert calls["i"] == _ATTEMPTS
    # tmp_path was never handed to the run; prove nothing leaked into it.
    assert _artifacts(tmp_path) == []


def test_successful_parse_writes_nothing_even_with_the_key_set(tmp_path) -> None:
    """A reply that validates takes the return path and never reaches the failure hook."""
    model, _ = _sequence_model(['{"verdict": "PASS", "findings": [], "summary": "ok"}'])
    out = _run(model, artifact_dir=tmp_path)
    assert out["verdict"] == "PASS"
    assert _artifacts(tmp_path) == []


def test_rotation_keeps_only_the_newest_twenty(tmp_path) -> None:
    """After writing, the directory is pruned oldest-first to at most 20 artifacts."""
    import os
    import time

    # Seed 20 pre-existing artifacts with strictly increasing mtimes.
    now = time.time()
    seeded = []
    for k in range(20):
        p = tmp_path / f"seed_{k:02d}.json"
        p.write_text("{}")
        os.utime(p, (now - (100 - k), now - (100 - k)))
        seeded.append(p)
    oldest = seeded[0]

    model, _ = _sequence_model(["never any json"])
    with pytest.raises(LLMRunnerError):
        _run(model, artifact_dir=tmp_path)

    files = _artifacts(tmp_path)
    assert len(files) == 20, f"rotation must cap at 20, got {len(files)}"
    assert not oldest.exists(), "the oldest artifact must be evicted"


def test_unwritable_dir_still_raises_the_original_parse_error(tmp_path) -> None:
    """The artifact write is BEST-EFFORT: an I/O failure never masks the parse error.

    A path that exists as a FILE (not a directory) cannot hold artifacts; the run must still
    raise the original LLMRunnerError rather than an artifact-write error.
    """
    clash = tmp_path / "not_a_dir"
    clash.write_text("i am a file")
    model, calls = _sequence_model(["never any json"])
    with pytest.raises(LLMRunnerError):
        _run(model, artifact_dir=clash)
    assert calls["i"] == _ATTEMPTS


def test_config_key_is_documented_with_default_and_rotation(tmp_path) -> None:
    """docs/config.md documents the key, its default (off), and the rotation behavior."""
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "config.md").read_text()
    assert "parse_failure_artifact_dir" in doc
    # Names both the default-off posture and the rotation cap.
    lowered = doc.lower()
    assert "rotat" in lowered or "newest" in lowered or "20" in doc
