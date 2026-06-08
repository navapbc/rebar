"""Tests for the _apply_inbound_clean_label leaf in dso_reconciler/applier.py.

Behavior under test:
  - Each `dso-id-*` label in payload['labels_to_remove'] triggers exactly one
    client.remove_label call, routed through _call_with_retry.
  - Positional args are pinned: (issue_key, label_string).
  - Non-`dso-id-*` labels are filtered out (defensive).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "applier.py"
)


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


def _make_clean_label_mutation(applier_mod, labels):
    mut_mod = applier_mod._load_mutation_module()
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.clean_label,
        target="PROJ-100",
        payload={"labels_to_remove": labels},
        provenance={"source": "test"},
    )


def test_remove_label_payload_pinned(applier):
    """remove_label is called once per dso-id-* label, with (issue_key, label) positional."""
    client = SimpleNamespace(remove_label=MagicMock(return_value=None))
    mutation = _make_clean_label_mutation(applier, ["dso-id-abc", "dso-id-xyz"])

    captured: list[tuple] = []
    real = applier._call_with_retry

    def spy(fn, *args, **kwargs):
        captured.append((fn, args, kwargs))
        return real(fn, *args, **kwargs)

    with patch.object(applier, "_call_with_retry", side_effect=spy):
        result = applier._apply_inbound_clean_label(mutation, client=client)

    remove_calls = [c for c in captured if c[0] is client.remove_label]
    assert len(remove_calls) == 2
    assert remove_calls[0][1] == ("PROJ-100", "dso-id-abc")
    assert remove_calls[1][1] == ("PROJ-100", "dso-id-xyz")
    # ApplyResult reports which labels were removed.
    assert result.payload == {"removed": ["dso-id-abc", "dso-id-xyz"]}


def test_non_dso_id_labels_skipped(applier):
    """Labels not starting with `dso-id-` are skipped defensively."""
    client = SimpleNamespace(remove_label=MagicMock(return_value=None))
    mutation = _make_clean_label_mutation(applier, ["foo", "dso-id-keep", "bar"])

    result = applier._apply_inbound_clean_label(mutation, client=client)

    assert client.remove_label.call_count == 1
    args, _kwargs = client.remove_label.call_args
    assert args == ("PROJ-100", "dso-id-keep")
    assert result.payload == {"removed": ["dso-id-keep"]}


def test_empty_payload_no_calls(applier):
    """An empty/missing labels_to_remove list results in zero client calls."""
    client = SimpleNamespace(remove_label=MagicMock(return_value=None))
    mutation = _make_clean_label_mutation(applier, [])

    result = applier._apply_inbound_clean_label(mutation, client=client)

    assert client.remove_label.call_count == 0
    assert result.payload == {"removed": []}
