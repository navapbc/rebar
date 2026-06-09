"""RED tests for ProvenanceLedger (story 26de)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LEDGER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "provenance_ledger.py"


def _load_ledger():
    spec = importlib.util.spec_from_file_location("provenance_ledger", LEDGER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["provenance_ledger"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def lm():
    return _load_ledger()


def test_record_exposes_side_and_timestamp(lm):
    """ProvenanceLedger.record writes an entry whose to_dict() output exposes 'side' and 'timestamp' keys per element."""
    ledger = lm.ProvenanceLedger()
    ledger.record("PROJ-1", "local", {"status": "open"})
    out = ledger.to_dict()
    entries = out["entries"]["PROJ-1"]
    assert len(entries) == 1
    assert "side" in entries[0]
    assert "timestamp" in entries[0]
    assert entries[0]["side"] == "local"


def test_is_echo_is_stateless_content_equality(lm):
    """is_echo returns True for content-hash-identical values without consulting record() history."""
    ledger = lm.ProvenanceLedger()
    ledger.record("PROJ-1", "local", {"status": "open"})
    # Identical content → echo
    assert ledger.is_echo("PROJ-1", {"status": "open"}) is True
    # Different content → not echo
    assert ledger.is_echo("PROJ-1", {"status": "closed"}) is False
    # Key with no history → not echo
    assert ledger.is_echo("UNKNOWN", {"status": "open"}) is False


def test_to_dict_is_json_serializable(lm):
    """to_dict() output is JSON-serializable via json.dumps() with no encoder."""
    ledger = lm.ProvenanceLedger()
    ledger.record("PROJ-1", "local", {"status": "open"})
    ledger.record("PROJ-1", "jira", {"status": "in_progress"})
    out = ledger.to_dict()
    # Round-trip without raising
    text = json.dumps(out)
    parsed = json.loads(text)
    assert parsed["schema_version"] == 1
    assert "entries" in parsed


def test_invalid_side_raises(lm):
    ledger = lm.ProvenanceLedger()
    with pytest.raises(ValueError, match="side must be"):
        ledger.record("PROJ-1", "neither", {"status": "open"})
