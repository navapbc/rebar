"""RED→GREEN tests for differ (inbound, probe) emission on absent jira partner."""

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "differ.py"
MUTATION_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mutation.py"


def _load(path, name):
    # CACHE-FIRST, matching the loader bug 9f0b installed in the sibling differ suites and bug
    # 2bc7's audit of the rest. This previously overwrote `sys.modules[name]` unconditionally and
    # returned the replacement: self-consistent within this file, but it CLOBBERS another file's
    # registration of the same shared key ("differ" and "mutation" are both loaded by several
    # modules). Nothing fails today only because pytest executes module-by-module, so each file's
    # module-scoped fixture is built while its own registration is current — the same
    # order-dependent coupling that made 2bc7 a macOS-only surprise.
    import sys

    cached = sys.modules.get(name)
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def differ():
    return _load(DIFFER_PATH, "differ")


@pytest.fixture(scope="module")
def mut():
    return _load(MUTATION_PATH, "mutation")


def test_emits_probe_for_absent_only(differ, mut):
    """Differ emits exactly one (inbound, probe, DIG-100) mutation for absent partner; zero for
    present partner.
    """
    local_state = {
        "LOCAL-X": {"local_id": "id-X", "jira_key": "DIG-100"},  # partner DIG-100 absent
        "LOCAL-Y": {"local_id": "id-Y", "jira_key": "DIG-200"},  # partner DIG-200 present
    }
    jira_state = {
        "DIG-200": {"local_id": "id-Y"},  # DIG-100 missing
    }
    mutations = differ.compute_mutations(local_state, jira_state)
    probe_muts = [
        m
        for m in mutations
        if m.direction == mut.MutationDirection.inbound and m.action == mut.MutationAction.probe
    ]
    assert len(probe_muts) == 1, f"expected 1 (inbound, probe), got {len(probe_muts)}: {probe_muts}"
    assert probe_muts[0].target == "DIG-100"
    # provenance should reference the local target
    prov = probe_muts[0].provenance or {}
    assert prov.get("local_target") == "LOCAL-X" or "LOCAL-X" in str(prov)
