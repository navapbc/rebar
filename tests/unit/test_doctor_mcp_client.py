"""``doctor`` MCP client-config diagnostics — ticket 71e7-4626-cb19-42d7.

The rebar MCP server disappears from a client's tool list for two silent reasons, and
neither is visible from inside the server:

  * the config names a bearer PAT env var that is **not set** in the environment the
    client was launched from — the exact shape of the documented one-off ``export``,
    which is persisted nowhere and dies with the shell it was typed in; and
  * the config names a bearer PAT env var that is **not the canonical one** for that
    client, so the operator's (correctly exported) canonical variable and the config's
    variable never meet — a fault that persists even when the misnamed variable happens
    to resolve.

Fixing either alone can leave the server omitted, so ``scan_mcp_clients`` must report
them independently. Every assertion here targets OBSERVABLE behaviour — the finding
dicts returned and the strings ``render_text`` renders — never source text or private
structure. The scan is stdlib-only and OS-agnostic, so these tests run anywhere
(``project.portability``).
"""

from __future__ import annotations

import json

import pytest

from rebar._commands import doctor_mcp_client as dmc

# Obvious fakes. Nothing here is or resembles a real credential.
FAKE_PAT = "FAKE-NOT-A-REAL-PAT-0000"


def _write_codex(home, *, env_var=dmc.CANONICAL_PAT_ENV["codex"]):
    """Plant a Codex ``~/.codex/config.toml`` naming ``env_var`` as its bearer source."""
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[mcp_servers.rebar]\n"
        'url = "https://example.invalid/mcp/"\n'
        f'bearer_token_env_var = "{env_var}"\n'
    )
    return path


def _write_header_client(home, client, *, env_var, template="Bearer ${{{name}}}"):
    """Plant a header-style (copilot/claude) config referencing ``env_var``."""
    relpath = {"copilot": ".copilot/mcp-config.json", "claude": ".claude.json"}[client]
    path = home / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "rebar": {
                        "type": "http",
                        "url": "https://example.invalid/mcp/",
                        "headers": {"Authorization": template.format(name=env_var)},
                    }
                }
            }
        )
    )
    return path


def _kinds(findings, client="codex"):
    return [f["kind"] for f in findings if f["client"] == client]


def test_canonical_name_and_set_variable_is_clean(tmp_path):
    """A correctly wired client must produce NEITHER headline finding.

    If this fails, ``doctor`` cries wolf on a healthy box and operators learn to ignore
    it — the one outcome that makes the diagnostic worse than nothing.
    """
    _write_codex(tmp_path)
    findings = dmc.scan_mcp_clients(home=tmp_path, env={dmc.CANONICAL_PAT_ENV["codex"]: FAKE_PAT})
    kinds = _kinds(findings)
    assert dmc.KIND_PAT_UNRESOLVABLE not in kinds
    assert dmc.KIND_STALE_PAT_ENV_NAME not in kinds
    assert kinds == [dmc.KIND_OK]


def test_unset_variable_reports_pat_unresolvable_naming_the_variable(tmp_path):
    """The canonical name with nothing exported must be reported, naming the variable.

    This is the production failure exactly: a transient ``export`` in a shell that has
    since exited. Without this finding the client just silently omits ``rebar`` and the
    operator has no signal at all to act on.
    """
    _write_codex(tmp_path)
    findings = dmc.scan_mcp_clients(home=tmp_path, env={})
    unresolvable = [f for f in findings if f["kind"] == dmc.KIND_PAT_UNRESOLVABLE]
    assert len(unresolvable) == 1
    assert unresolvable[0]["client"] == "codex"
    assert dmc.CANONICAL_PAT_ENV["codex"] in unresolvable[0]["detail"]


def test_empty_variable_counts_as_unresolvable(tmp_path):
    """An exported-but-empty variable must be reported, not treated as configured.

    ``export VAR=`` authenticates with an empty bearer and fails exactly like an unset
    one; passing it would let doctor certify a box that cannot connect.
    """
    _write_codex(tmp_path)
    findings = dmc.scan_mcp_clients(home=tmp_path, env={dmc.CANONICAL_PAT_ENV["codex"]: "   "})
    assert dmc.KIND_PAT_UNRESOLVABLE in _kinds(findings)


def test_stale_env_name_reported_and_names_the_canonical_variable(tmp_path):
    """A non-canonical bearer variable must be reported with the migration target.

    This is the compounding half of the production fault: a config reading a name that
    appears nowhere in this project. Without the canonical name in the detail the
    operator is told something is wrong but not what to change it to.
    """
    _write_codex(tmp_path, env_var="REBAR_CODEX_PAT")
    findings = dmc.scan_mcp_clients(home=tmp_path, env={})
    stale = [f for f in findings if f["kind"] == dmc.KIND_STALE_PAT_ENV_NAME]
    assert len(stale) == 1
    assert dmc.CANONICAL_PAT_ENV["codex"] in stale[0]["detail"]


def test_stale_env_name_fires_even_when_the_stale_variable_resolves(tmp_path):
    """A resolvable-but-misnamed variable is still wrong and must still be reported.

    If staleness were suppressed by resolution, an operator who exported the stale name
    once would be told the box is healthy — and the wiring would break again the moment
    they follow the documented (canonical) setup on a new machine.
    """
    _write_codex(tmp_path, env_var="REBAR_CODEX_PAT")
    findings = dmc.scan_mcp_clients(home=tmp_path, env={"REBAR_CODEX_PAT": FAKE_PAT})
    kinds = _kinds(findings)
    assert dmc.KIND_STALE_PAT_ENV_NAME in kinds
    assert dmc.KIND_PAT_UNRESOLVABLE not in kinds


def test_stale_and_unresolvable_are_reported_independently(tmp_path):
    """Both headline faults must surface together when both hold.

    Fixing one alone leaves the server omitted, so collapsing them into a single finding
    would send the operator round the loop twice.
    """
    _write_codex(tmp_path, env_var="REBAR_CODEX_PAT")
    kinds = _kinds(dmc.scan_mcp_clients(home=tmp_path, env={}))
    assert dmc.KIND_STALE_PAT_ENV_NAME in kinds
    assert dmc.KIND_PAT_UNRESOLVABLE in kinds


def test_missing_config_degrades_to_a_finding_not_an_exception(tmp_path):
    """An unconfigured client must degrade, never raise.

    ``doctor`` runs on every developer box; an exception here would take the whole
    command down for anyone who does not use all three clients.
    """
    findings = dmc.scan_mcp_clients(home=tmp_path, env={})
    assert {f["client"] for f in findings} == set(dmc.CLIENT_ORDER)
    assert all(f["kind"] == dmc.KIND_CONFIG_MISSING for f in findings)
    assert all(f["severity"] == dmc.SEVERITY_UNAVAILABLE for f in findings)
    assert not dmc.has_blocking_mcp_client(findings)


@pytest.mark.parametrize(
    ("relpath", "body"),
    [
        (".codex/config.toml", "[mcp_servers.rebar\nurl = "),
        (".copilot/mcp-config.json", "{not json"),
        (".claude.json", "{not json"),
    ],
)
def test_malformed_config_degrades_to_a_finding_not_an_exception(tmp_path, relpath, body):
    """An unparseable config must be reported, never crash the scan.

    A half-edited config is the single most likely state to run ``doctor`` in; raising
    there denies the operator the diagnostic precisely when they need it.
    """
    path = tmp_path / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    findings = dmc.scan_mcp_clients(home=tmp_path, env={})
    unreadable = [f for f in findings if f["kind"] == dmc.KIND_CONFIG_UNREADABLE]
    assert len(unreadable) == 1
    assert str(path) in unreadable[0]["detail"]


def test_config_without_a_rebar_entry_is_reported(tmp_path):
    """A client config with no ``rebar`` server must be reported, not read as healthy.

    Otherwise a config that was never merged looks identical to a working one and the
    operator debugs the credential instead of the missing entry.
    """
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[mcp_servers.other]\nurl = "https://example.invalid/mcp/"\n')
    assert dmc.KIND_SERVER_ABSENT in _kinds(dmc.scan_mcp_clients(home=tmp_path, env={}))


@pytest.mark.parametrize("client", ["copilot", "claude"])
def test_header_clients_resolve_both_dollar_forms(tmp_path, client):
    """``Bearer $VAR`` (Copilot) and ``Bearer ${VAR}`` (Claude) must both resolve.

    Reading the documented form of either client as a literal would report a bogus
    embedded-credential fault on a correctly configured box.
    """
    canonical = dmc.CANONICAL_PAT_ENV[client]
    template = "Bearer ${name}" if client == "copilot" else "Bearer ${{{name}}}"
    _write_header_client(tmp_path, client, env_var=canonical, template=template)
    kinds = _kinds(dmc.scan_mcp_clients(home=tmp_path, env={canonical: FAKE_PAT}), client)
    assert kinds == [dmc.KIND_OK]


def test_header_clients_report_a_stale_name(tmp_path):
    """A header client naming a non-canonical variable must be caught too.

    The canonical-name table covers all three clients; a gap would let the same fault
    hide behind Copilot or Claude Code instead of Codex.
    """
    _write_header_client(tmp_path, "copilot", env_var="SOME_OTHER_PAT", template="Bearer ${name}")
    findings = dmc.scan_mcp_clients(home=tmp_path, env={"SOME_OTHER_PAT": FAKE_PAT})
    stale = [f for f in findings if f["kind"] == dmc.KIND_STALE_PAT_ENV_NAME]
    assert [f["client"] for f in stale] == ["copilot"]
    assert dmc.CANONICAL_PAT_ENV["copilot"] in stale[0]["detail"]


def test_literal_bearer_in_config_is_reported_without_echoing_it(tmp_path):
    """A credential literal in a config must be flagged — and never reproduced.

    Echoing the header would copy the secret into doctor's output, logs, and any CI
    artifact that captures them: the diagnostic would become the leak.
    """
    _write_header_client(tmp_path, "claude", env_var="ignored", template="Bearer " + FAKE_PAT)
    findings = dmc.scan_mcp_clients(home=tmp_path, env={})
    literal = [f for f in findings if f["kind"] == dmc.KIND_PAT_LITERAL]
    assert len(literal) == 1
    assert FAKE_PAT not in json.dumps(findings)
    assert FAKE_PAT not in "\n".join(dmc.render_text(findings))


def test_no_credential_value_reaches_findings_or_rendered_text(tmp_path):
    """No resolved PAT value may appear in any finding or rendered line, ever.

    ``doctor`` output is pasted into tickets, chat, and CI logs. A value leaking here
    would turn a diagnostic run into a credential disclosure requiring rotation.
    """
    _write_codex(tmp_path, env_var="REBAR_CODEX_PAT")
    _write_header_client(
        tmp_path,
        "copilot",
        env_var=dmc.CANONICAL_PAT_ENV["copilot"],
        template="Bearer ${name}",
    )
    env = {
        "REBAR_CODEX_PAT": FAKE_PAT,
        dmc.CANONICAL_PAT_ENV["copilot"]: FAKE_PAT,
        dmc.CANONICAL_PAT_ENV["claude"]: FAKE_PAT,
    }
    findings = dmc.scan_mcp_clients(home=tmp_path, env=env)
    blob = json.dumps(findings) + "\n".join(dmc.render_text(findings))
    assert FAKE_PAT not in blob
    # The diagnostic is only useful if it names the variables it checked.
    assert "REBAR_CODEX_PAT" in blob
    assert dmc.CANONICAL_PAT_ENV["codex"] in blob


def test_render_text_emits_a_header_and_one_line_per_finding(tmp_path):
    """Rendering must always produce a section header plus every finding.

    The caller prints these lines verbatim; a silent render would make an unconfigured
    box indistinguishable from a check that never ran.
    """
    findings = dmc.scan_mcp_clients(home=tmp_path, env={})
    lines = list(dmc.render_text(findings))
    assert lines[0] == "doctor: mcp clients"
    assert len(lines) == len(findings) + 1
    assert all(isinstance(line, str) for line in lines)


def test_headline_findings_classify_as_blocking_severity(tmp_path):
    """Both headline faults must classify as errors via ``has_blocking_mcp_client``.

    That predicate is the seam a caller gates on. If a broken PAT wiring classified as
    advisory, any check built on it would pass a box whose client cannot reach the
    server at all.
    """
    _write_codex(tmp_path, env_var="REBAR_CODEX_PAT")
    assert dmc.has_blocking_mcp_client(dmc.scan_mcp_clients(home=tmp_path, env={}))


def test_healthy_and_unconfigured_boxes_do_not_classify_as_blocking(tmp_path):
    """Neither a clean wiring nor an unconfigured client may classify as an error.

    A predicate that fired on an absent config would make every box that does not run
    all three clients look broken.
    """
    assert not dmc.has_blocking_mcp_client(dmc.scan_mcp_clients(home=tmp_path, env={}))
    _write_codex(tmp_path)
    healthy = dmc.scan_mcp_clients(home=tmp_path, env={dmc.CANONICAL_PAT_ENV["codex"]: FAKE_PAT})
    assert not dmc.has_blocking_mcp_client(healthy)


def test_env_defaults_to_the_process_environment(monkeypatch, tmp_path):
    """Omitting ``env`` must consult the real process environment.

    ``doctor`` calls ``scan_mcp_clients()`` with no arguments; if the default did not
    read ``os.environ`` the shipped command would report every client as unresolvable.
    """
    _write_codex(tmp_path)
    monkeypatch.setenv(dmc.CANONICAL_PAT_ENV["codex"], FAKE_PAT)
    assert _kinds(dmc.scan_mcp_clients(home=tmp_path)) == [dmc.KIND_OK]
