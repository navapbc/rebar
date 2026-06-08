"""RED tests for inbound_probe (story 60c3)."""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PROBE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "inbound_probe.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("inbound_probe", PROBE_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["inbound_probe"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def probe_mod():
    return _load_probe()


def test_classifies_present_resolved(probe_mod):
    r = probe_mod.classify_probe_response("PROJ-1", 200, {"fields": {"status": {"name": "Done"}}})
    assert r.branch == probe_mod.ProbeBranch.PRESENT_RESOLVED


def test_classifies_present_filtered(probe_mod):
    r = probe_mod.classify_probe_response("PROJ-2", 200, {"fields": {"status": {"name": "In Progress"}}})
    assert r.branch == probe_mod.ProbeBranch.PRESENT_FILTERED


def test_classifies_archived_or_moved(probe_mod):
    for code in (404, 410, 403):
        r = probe_mod.classify_probe_response("PROJ-3", code, {})
        assert r.branch == probe_mod.ProbeBranch.ARCHIVED_OR_MOVED, code


def test_classifies_unreachable(probe_mod):
    for code in (500, 502, 503, 401):
        r = probe_mod.classify_probe_response("PROJ-4", code, {})
        assert r.branch == probe_mod.ProbeBranch.UNREACHABLE, code


def test_request_is_get_only(probe_mod):
    req = probe_mod._make_request("https://example.atlassian.net", "PROJ-1", "user", "tok")
    assert req.get_method() == "GET"


def test_missing_env_raises_probe_config_error(probe_mod, monkeypatch):
    for var in ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(probe_mod.ProbeConfigError, match="inbound_probe: missing required env var"):
        probe_mod._resolve_env()
