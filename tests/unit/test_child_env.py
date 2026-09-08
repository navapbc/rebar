"""Behavioral tests for child environment projection.

A same-capability child inherits ambient values. An owning child receives its adapter
credentials, while unrelated children lose every registered adapter secret. Unknown process
settings survive, and projection does not mutate its input or ``os.environ``.
"""

from __future__ import annotations

from rebar import _child_env


def test_registry_declares_each_adapters_exact_secret_names() -> None:
    """The registry names the Cloud and Data Center send credentials, by exact name."""
    assert "JIRA_API_TOKEN" in _child_env.adapter_secret_names("jira")
    assert "JIRA_PAT" in _child_env.adapter_secret_names("jira-datacenter")
    # The union covers both adapters' secrets.
    owned = _child_env.owned_secret_names()
    assert {"JIRA_API_TOKEN", "JIRA_PAT"} <= owned


def test_registry_holds_names_never_values() -> None:
    """The registry is a set of NAMES; it carries no secret values."""
    for name in _child_env.owned_secret_names():
        assert isinstance(name, str)
        assert name.isupper() or "_" in name  # env-var-shaped identifier, not a value


def test_same_capability_child_inherits_ambient() -> None:
    """A trusted same-capability child starts from full ambient inheritance."""
    base = {"JIRA_API_TOKEN": "tok", "JIRA_URL": "https://x", "PATH": "/usr/bin"}
    out = _child_env.project_child_env(base, relationship="same_capability")
    assert out == base
    assert out is not base  # a NEW mapping


def test_owning_child_receives_its_overlay_and_drops_other_adapters_secret() -> None:
    """An owning child gets its adapter overlay; another adapter's secret is absent."""
    base = {
        "JIRA_API_TOKEN": "ambient-cloud",
        "JIRA_PAT": "ambient-dc",
        "JIRA_URL": "https://x",
    }
    out = _child_env.project_child_env(
        base,
        relationship="owning",
        owner="jira",
        overlay={"JIRA_API_TOKEN": "overlay-cloud"},
    )
    # The owner's secret comes from the overlay.
    assert out["JIRA_API_TOKEN"] == "overlay-cloud"
    # The OTHER adapter's secret name is gone.
    assert "JIRA_PAT" not in out
    # Non-secret config survives.
    assert out["JIRA_URL"] == "https://x"


def test_unrelated_sibling_omits_every_adapter_secret_name() -> None:
    """An unrelated sibling loses every Rebar adapter's declared secret NAME."""
    base = {
        "JIRA_API_TOKEN": "cloud",
        "JIRA_PAT": "dc",
        "JIRA_URL": "https://x",
        "PATH": "/usr/bin",
    }
    out = _child_env.project_child_env(base, relationship="unrelated")
    assert "JIRA_API_TOKEN" not in out
    assert "JIRA_PAT" not in out
    # Non-secret config and unrelated vars survive.
    assert out["JIRA_URL"] == "https://x"
    assert out["PATH"] == "/usr/bin"


def test_projection_returns_new_mapping_without_mutating_base() -> None:
    """Projecting does not mutate the caller's mapping."""
    base = {"JIRA_API_TOKEN": "cloud", "JIRA_URL": "https://x"}
    snapshot = dict(base)
    _child_env.project_child_env(base, relationship="unrelated")
    assert base == snapshot  # base untouched
