"""Capture the returned Jira comment ID at both enactment sites (emersed-specific-mutt).

``add_comment`` returns ``{"id": ...}`` and today both enactment loops discard it.
This story captures it and persists it against the comment entry's
``local_comment_key`` via ``BindingStore.record_comment_id`` so a re-sync never
re-posts the comment. Both the CREATE leaf (``create_one``) and the UPDATE
comment dispatch (``_update_one_dispatch_comments``) must record.

Hermetic: a ``MagicMock`` Jira client (no network) + a real ``BindingStore`` over
a tmp tracker dir.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SRC_DIR = Path(__file__).resolve().parents[4] / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SRC_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def binding_store_mod():
    return _load("_binding_store_for_capture", "binding_store.py")


@pytest.fixture()
def store(binding_store_mod, tmp_path):
    return binding_store_mod.BindingStore(tmp_path / ".tickets-tracker")


_KEY = "1785800303802040001-cccc"


def test_update_dispatch_records_comment_id(store):
    """_update_one_dispatch_comments records the returned id against the key."""
    from rebar_reconciler.dispatch_apply_phases import _update_one_dispatch_comments

    client = MagicMock()
    client.add_comment.return_value = {"id": "10001"}
    mutation = {"comments": [{"body": "hello", "local_comment_key": _KEY}]}

    _update_one_dispatch_comments(mutation, client, "DIG-1", [], binding_store=store)

    client.add_comment.assert_called_once()
    assert store.is_comment_mapped(_KEY) is True
    assert store.comment_id_for(_KEY) == "10001"


def test_update_dispatch_without_store_is_noop(store):
    """binding_store is optional — the legacy path still posts the comment."""
    from rebar_reconciler.dispatch_apply_phases import _update_one_dispatch_comments

    client = MagicMock()
    client.add_comment.return_value = {"id": "10001"}
    mutation = {"comments": [{"body": "hello", "local_comment_key": _KEY}]}

    _update_one_dispatch_comments(mutation, client, "DIG-1", [])
    client.add_comment.assert_called_once()


def test_create_one_records_comment_id(store):
    """create_one captures the add_comment id and persists it via the store."""
    from rebar_reconciler.dispatch_one import create_one

    client = MagicMock()
    client.search_issues.return_value = []  # JQL miss -> create
    client.create_issue.return_value = {"key": "DIG-2", "id": "500"}
    client.add_comment.return_value = {"id": "20002"}
    mutation = {
        "action": "create",
        "local_id": "tick-cap1",
        "fields": {"summary": "S", "issuetype": {"name": "Task"}},
        "comments": [{"body": "created comment", "local_comment_key": _KEY}],
    }

    create_one(mutation, client, binding_store=store)

    assert store.comment_id_for(_KEY) == "20002"


def test_re_sync_skips_already_mapped(store):
    """After recording, a second dispatch of the same key does not re-post
    (idempotent append-only) — exercised via the differ's skip in the diff test;
    here we assert the store answers is_comment_mapped, the skip's input."""
    store.record_comment_id(_KEY, "20002")
    assert store.is_comment_mapped(_KEY) is True


def test_comment_id_captured_and_persisted(store):
    """AC: the returned ``add_comment`` ``{"id": ...}`` is captured at BOTH
    enactment sites — ``create_one`` and ``update_one`` →
    ``_update_one_dispatch_comments`` — and persisted against the entry's
    ``local_comment_key`` via ``record_comment_id``. The enactment code is
    backend-agnostic, so the same capture holds whether the client is the Cloud
    (acli) transport or the DC transport (both return ``{"id": ...}``)."""
    from rebar_reconciler.dispatch_apply_phases import _update_one_dispatch_comments
    from rebar_reconciler.dispatch_one import create_one

    create_key = "1785800303802040001-create"
    update_key = "1785800303802040001-update"

    # Enactment site 1: create_one (models the Cloud/acli create leaf).
    cloud_client = MagicMock()
    cloud_client.search_issues.return_value = []
    cloud_client.create_issue.return_value = {"key": "DIG-9", "id": "900"}
    cloud_client.add_comment.return_value = {"id": "30001"}
    create_one(
        {
            "action": "create",
            "local_id": "tick-both",
            "fields": {"summary": "S", "issuetype": {"name": "Task"}},
            "comments": [{"body": "on create", "local_comment_key": create_key}],
        },
        cloud_client,
        binding_store=store,
    )
    assert store.comment_id_for(create_key) == "30001"

    # Enactment site 2: update dispatch (models the DC transport update leaf).
    dc_client = MagicMock()
    dc_client.add_comment.return_value = {"id": "30002"}
    _update_one_dispatch_comments(
        {"comments": [{"body": "on update", "local_comment_key": update_key}]},
        dc_client,
        "DIG-9",
        [],
        binding_store=store,
    )
    assert store.comment_id_for(update_key) == "30002"
