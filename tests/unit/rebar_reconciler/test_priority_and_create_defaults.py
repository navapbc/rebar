"""Config-driven priority mapping + str-valued per-project create_defaults — the S5
outbound seam (epic ravenous-dirt-widgeon / a881-2d9a-16bf-4acb).

Priority is a SOFT axis: an unmapped priority DRIFTS (leave Jira's value, omit the
field, warn once) — it never fails the sync and is never coerced to ``"Medium"``. It has
NO vocabulary axis, so there is no fail-closed claim for priority (unlike link/type).
``create_defaults`` is a str-valued per-project axis merged into the CREATE bodies for
required-beyond-baseline fields; baseline computed fields win on collision; UPDATE applies
none.

Assertions target OBSERVABLE behaviour and contracts only — resolved mapping values, the
vendor-shaped payload dicts the CREATE/UPDATE mappers emit, the omitted field on drift,
the stderr drift alert, and the built-in parity — never private structure. Every test
drives a REAL entry point:

  * ``config.effective_priority_map`` / ``config.effective_create_defaults`` (resolution)
  * ``JiraBackend.map_local_to_remote`` (the Cloud CREATE mapper)
  * ``_DCOutbound.map_local_to_remote`` (the Data Center CREATE mapper)
  * ``OutboundFieldMapper.map_fields_to_remote`` / ``compute_update_fields`` (UPDATE)
  * ``outbound_mapper.resolve_outbound_priority`` (the shared map-or-drift resolver)

built-in behaviour (no ``[mapping]`` present) must stay bit-for-bit what it is today for
every in-range priority (0-4 -> Highest/High/Medium/Low/Lowest).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rebar import config as user_cfg
from rebar_reconciler import config as cfg_mod
from rebar_reconciler.adapters.jira_family.value_maps import LOCAL_PRIORITY_TO_JIRA

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures — isolated config discovery + a repo root carrying a [mapping] block
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No real user config may leak a ``[mapping]`` section into these tests."""
    xdg = tmp_path / "xdg"
    (xdg / "rebar").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    for name in ("REBAR_CONFIG", "REBAR_ROOT", "REBAR_CONFIG_UNKNOWN_KEYS"):
        monkeypatch.delenv(name, raising=False)
    user_cfg.set_cli_overrides(None)
    user_cfg.reset_config_cache()


@pytest.fixture(autouse=True)
def _reset_drift() -> None:
    """Clear the per-process drift-warning dedupe set so emission counts are
    deterministic across tests."""
    from rebar_reconciler.adapters.jira_family import outbound_mapper

    outbound_mapper.reset_drift_warnings()


def _proj(tmp: Path, mapping_toml: str = "", *, name: str = "proj") -> Path:
    """A repo root whose discovered ``rebar.toml`` carries ``mapping_toml`` verbatim."""
    p = tmp / name
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    (p / "rebar.toml").write_text(mapping_toml, encoding="utf-8")
    user_cfg.reset_config_cache()
    return p


class _NullCodec:
    """A minimal ``RichTextCodec`` — priority/create_defaults never touch rich text."""

    def fit_outbound(self, value: object) -> object:
        return value

    def normalize_outbound(self, value: object) -> object:
        return value

    def decode_inbound(self, value: object) -> object:
        return value


def _cloud() -> Any:
    from rebar_reconciler.adapters.jira.backend import JiraBackend

    return JiraBackend(_NullCodec())  # type: ignore[arg-type]


def _dc() -> Any:
    from rebar_reconciler.adapters.jira_datacenter.backend import _DCOutbound

    return _DCOutbound()


def _mapper() -> Any:
    from rebar_reconciler.adapters.jira_family.outbound_mapper import OutboundFieldMapper

    return OutboundFieldMapper(_NullCodec())  # type: ignore[arg-type]


def _ticket(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "T-1",
        "description": "",
        "ticket_type": "task",
        "priority": 2,
        "status": "open",
        "assignee": "",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# HAPPY PATH (visible to the implementer)
# ---------------------------------------------------------------------------


def test_effective_priority_map_default_equals_builtin(tmp_path: Path) -> None:
    """With NO ``[mapping]`` block the effective priority map equals the built-in
    str-keyed ``config.local_to_jira_priority`` verbatim, and a per-project
    ``priority_map`` overlay remaps one key while leaving every un-overlaid key at its
    built-in target — the config seam is inert until configured. (Happy path: the
    no-config invariant + one remap. Config priority_map keys are STRINGS.)"""
    base = _proj(tmp_path, "", name="base")
    eff = cfg_mod.effective_priority_map("REB", root=base)
    assert eff == dict(cfg_mod.local_to_jira_priority)

    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.priority_map]\n"2" = "P2"\n',
        name="over",
    )
    eff2 = cfg_mod.effective_priority_map("REB", root=proj)
    assert eff2["2"] == "P2"  # overlaid key remapped
    assert eff2["0"] == cfg_mod.local_to_jira_priority["0"]  # unnamed key unchanged


def test_priority_map_threaded_through_create_and_update(tmp_path: Path) -> None:
    """The effective priority map reaches the REAL Cloud CREATE mapper and the shared
    UPDATE mapper: both accept a ``priority_map=`` kwarg and emit the overlaid name;
    passing ``priority_map=None`` falls back to today's built-in behaviour. (Happy path
    drives Cloud + update; the parity census covers the DC create slot held out.)"""
    proj = _proj(tmp_path, '[mapping.projects.REB.priority_map]\n"2" = "P2"\n')
    eff = cfg_mod.effective_priority_map("REB", root=proj)

    created = _cloud().map_local_to_remote(_ticket(priority=2), priority_map=eff)
    assert created["priority"] == "P2"

    updated = _mapper().map_fields_to_remote({"priority": 2}, priority_map=eff)
    assert updated["priority"] == "P2"

    # priority_map=None preserves the built-in mapping (0-4 -> Highest..Lowest).
    builtin_create = _cloud().map_local_to_remote(_ticket(priority=1), priority_map=None)
    assert builtin_create["priority"] == "High"


def test_create_defaults_injected_into_create(tmp_path: Path) -> None:
    """The Cloud CREATE mapper accepts a resolved str-valued ``create_defaults=`` dict
    and merges a required-beyond-baseline field into the create body. (Happy path: a
    non-colliding default is injected; collision precedence + DC parity are held out.)"""
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.create_defaults]\ncustomfield_10001 = "Platform"\n',
    )
    defaults = cfg_mod.effective_create_defaults("REB", root=proj)
    assert defaults["customfield_10001"] == "Platform"

    created = _cloud().map_local_to_remote(_ticket(), create_defaults=defaults)
    assert created["customfield_10001"] == "Platform"


# ---------------------------------------------------------------------------
# HELD-OUT ORACLE — edge / contract / parity (withheld from the implementer)
# ---------------------------------------------------------------------------


def test_priority_map_reaches_dc_create(tmp_path: Path) -> None:
    """Parity census (DC create slot): the effective priority map reaches the Data
    Center CREATE mapper the same way it reaches Cloud — ``_DCOutbound.map_local_to_
    remote`` accepts ``priority_map=`` and emits the overlaid name. The shared resolver
    means all three payload consumers (Cloud create, DC create, update) agree."""
    proj = _proj(tmp_path, '[mapping.projects.REB.priority_map]\n"2" = "P2"\n')
    eff = cfg_mod.effective_priority_map("REB", root=proj)

    created = _dc().map_local_to_remote(_ticket(priority=2), priority_map=eff)
    assert created["priority"] == "P2"


def test_priority_drifts_never_coerced_to_medium(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A priority with NO effective target DRIFTS: the CREATE body OMITS the
    ``priority`` field entirely (Jira left as-is) — never coerced to ``"Medium"`` — a
    non-fatal warning naming the priority is emitted, and the mapper does NOT raise
    (priority is a soft, non-fail-closed axis). An out-of-range priority has no built-in
    target, so this fires even with ``priority_map=None``: the map-or-drift rule replaces
    the old unconditional ``"Medium"`` fallback."""
    proj = _proj(tmp_path, "")
    _ = cfg_mod.effective_priority_map("REB", root=proj)

    created = _cloud().map_local_to_remote(_ticket(priority=7), priority_map=None)
    assert "priority" not in created  # DRIFT: field omitted, never "Medium"
    assert "Medium" not in created.values()

    err = capsys.readouterr().err
    assert "7" in err  # a non-fatal drift alert names the drifting priority


def test_priority_drift_warning_deduped(capsys: pytest.CaptureFixture[str]) -> None:
    """A persistently drifting priority does not re-flood stderr: the drift alert is
    emitted at most once per distinct drifted priority per process, so a stuck priority
    warns once, not once-per-mapper-call. Driven through the shared resolver."""
    from rebar_reconciler.adapters.jira_family import outbound_mapper

    outbound_mapper.reset_drift_warnings()
    for _ in range(3):
        assert outbound_mapper.resolve_outbound_priority(7, None) is None
    err = capsys.readouterr().err
    # Exactly one drift line for the repeated same priority.
    assert err.count("7") == 1


def test_create_defaults_baseline_wins_and_reaches_dc(tmp_path: Path) -> None:
    """Collision precedence + DC parity: resolved ``create_defaults`` merge into BOTH
    create bodies (Cloud + DC), but a baseline computed field WINS on collision — a
    default that names ``summary``/``priority`` never overrides the value the mapper
    computes from the ticket, while a required-beyond-baseline field is still injected."""
    proj = _proj(
        tmp_path,
        "[mapping.projects.REB.create_defaults]\n"
        'summary = "SHOULD_NOT_WIN"\n'
        'customfield_10001 = "Platform"\n',
    )
    defaults = cfg_mod.effective_create_defaults("REB", root=proj)

    for created in (
        _cloud().map_local_to_remote(_ticket(title="Real title"), create_defaults=defaults),
        _dc().map_local_to_remote(_ticket(title="Real title"), create_defaults=defaults),
    ):
        # Baseline computed field wins on collision ...
        assert created["summary"] == "Real title"
        # ... and the non-colliding default is still injected.
        assert created["customfield_10001"] == "Platform"


def test_create_defaults_not_applied_on_update(tmp_path: Path) -> None:
    """``create_defaults`` is CREATE-only: the UPDATE mapper carries NO
    ``create_defaults`` seam at all (only ``priority_map``), so an update payload can
    only ever contain the mapped changed fields — a create-default field can never leak
    onto an edit. Asserted at the shared UPDATE boundary the differ routes edits through."""
    import inspect

    from rebar_reconciler.adapters.jira_family.outbound_mapper import OutboundFieldMapper

    params = inspect.signature(OutboundFieldMapper.map_fields_to_remote).parameters
    assert "priority_map" in params  # priority IS config-driven on update ...
    assert "create_defaults" not in params  # ... but create_defaults is create-only

    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.priority_map]\n"1" = "P1"\n',
    )
    eff = cfg_mod.effective_priority_map("REB", root=proj)
    out = _mapper().map_fields_to_remote({"priority": 1}, priority_map=eff)
    # Only the mapped changed field is present; no create-default field appears.
    assert out == {"priority": "P1"}


def test_local_to_jira_priority_parity() -> None:
    """``config.local_to_jira_priority`` (the str-keyed baseline the effective map is
    built from) must stay in lock-step with the adapter-side int-keyed
    ``value_maps.LOCAL_PRIORITY_TO_JIRA`` — same targets, keyed by ``str(level)`` — the
    parity that keeps the two independent literals honest, exactly as
    ``jira_to_local_status`` / ``jira_to_local_type`` do for their axes."""
    assert cfg_mod.local_to_jira_priority == {str(k): v for k, v in LOCAL_PRIORITY_TO_JIRA.items()}
