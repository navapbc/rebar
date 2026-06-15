"""Schema fitness tests for rebar_reconciler/health.py record_pass().

Verifies that the JSON written by record_pass() contains exactly the 7
required fields with correct types and values — no more, no less.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading (mirrors pattern from test_health.py)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
HEALTH_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "health.py"


def _load_health() -> ModuleType:
    spec = importlib.util.spec_from_file_location("health", HEALTH_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def health() -> ModuleType:
    return _load_health()


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_PASS_ID = "fitness-pass-001"
_PRE_FSCK = 5
_POST_FSCK = 3
_PER_TYPE_COUNTS = {"epic": 1, "story": 2, "task": 1, "bug": 0}
_LOCAL_MUTATION_COUNT = 7

_REQUIRED_FIELDS = {
    "schema_version",
    "pass_id",
    "pre_pass_fsck_total",
    "post_pass_fsck_total",
    "per_type_open_counts",
    "local_mutation_count_at_pass",
    "timestamp_ns",
}


def _call_record_pass(health: ModuleType, tmp_path: Path) -> dict:
    """Call record_pass() with canonical fitness data and return parsed JSON."""
    health.record_pass(
        pass_id=_PASS_ID,
        pre_fsck=_PRE_FSCK,
        post_fsck=_POST_FSCK,
        per_type_counts=_PER_TYPE_COUNTS,
        local_mutation_count=_LOCAL_MUTATION_COUNT,
        repo_root=tmp_path,
    )
    record_path = tmp_path / "bridge_state" / "health" / f"{_PASS_ID}.json"
    return json.loads(record_path.read_text())


# ---------------------------------------------------------------------------
# Schema fitness tests
# ---------------------------------------------------------------------------


def test_schema_version_is_int_and_equals_1(health: ModuleType, tmp_path: Path) -> None:
    """schema_version is an int equal to 1."""
    data = _call_record_pass(health, tmp_path)
    assert isinstance(data["schema_version"], int), (
        f"schema_version should be int, got {type(data['schema_version'])}"
    )
    assert data["schema_version"] == 1


def test_pass_id_is_str_and_matches(health: ModuleType, tmp_path: Path) -> None:
    """pass_id is a str equal to the value passed in."""
    data = _call_record_pass(health, tmp_path)
    assert isinstance(data["pass_id"], str), f"pass_id should be str, got {type(data['pass_id'])}"
    assert data["pass_id"] == _PASS_ID


def test_pre_pass_fsck_total_is_int_and_matches(health: ModuleType, tmp_path: Path) -> None:
    """pre_pass_fsck_total is an int equal to pre_fsck argument."""
    data = _call_record_pass(health, tmp_path)
    assert isinstance(data["pre_pass_fsck_total"], int), (
        f"pre_pass_fsck_total should be int, got {type(data['pre_pass_fsck_total'])}"
    )
    assert data["pre_pass_fsck_total"] == _PRE_FSCK


def test_post_pass_fsck_total_is_int_and_matches(health: ModuleType, tmp_path: Path) -> None:
    """post_pass_fsck_total is an int equal to post_fsck argument."""
    data = _call_record_pass(health, tmp_path)
    assert isinstance(data["post_pass_fsck_total"], int), (
        f"post_pass_fsck_total should be int, got {type(data['post_pass_fsck_total'])}"
    )
    assert data["post_pass_fsck_total"] == _POST_FSCK


def test_per_type_open_counts_is_dict_with_all_four_keys(
    health: ModuleType, tmp_path: Path
) -> None:
    """per_type_open_counts is a dict containing all four required type keys."""
    data = _call_record_pass(health, tmp_path)
    counts = data["per_type_open_counts"]
    assert isinstance(counts, dict), f"per_type_open_counts should be dict, got {type(counts)}"
    for key in ("epic", "story", "task", "bug"):
        assert key in counts, f"per_type_open_counts missing key '{key}'"
    assert counts == _PER_TYPE_COUNTS


def test_local_mutation_count_at_pass_is_int_and_matches(
    health: ModuleType, tmp_path: Path
) -> None:
    """local_mutation_count_at_pass is an int equal to local_mutation_count argument."""
    data = _call_record_pass(health, tmp_path)
    assert isinstance(data["local_mutation_count_at_pass"], int), (
        "local_mutation_count_at_pass should be int, "
        f"got {type(data['local_mutation_count_at_pass'])}"
    )
    assert data["local_mutation_count_at_pass"] == _LOCAL_MUTATION_COUNT


def test_timestamp_ns_is_positive_int(health: ModuleType, tmp_path: Path) -> None:
    """timestamp_ns is an int greater than zero."""
    data = _call_record_pass(health, tmp_path)
    ts = data["timestamp_ns"]
    assert isinstance(ts, int), f"timestamp_ns should be int, got {type(ts)}"
    assert ts > 0, f"timestamp_ns should be positive, got {ts}"


def test_no_extra_fields_in_schema(health: ModuleType, tmp_path: Path) -> None:
    """The written JSON contains exactly the 7 required fields and no others."""
    data = _call_record_pass(health, tmp_path)
    actual_fields = set(data.keys())
    extra = actual_fields - _REQUIRED_FIELDS
    missing = _REQUIRED_FIELDS - actual_fields
    assert not extra, f"Unexpected extra fields in health record: {extra}"
    assert not missing, f"Missing required fields in health record: {missing}"
