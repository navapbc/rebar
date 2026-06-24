"""In-editor JSON validation (story 998e): the testable Python core.

``validate_node_config`` validates one editor node's raw config JSON against its INPUT
contract and returns the DEFINED shape ``{ok, errors:[{path,message}], unavailable}``.
The four axes are kept DISTINCT and each is covered here:

  * EMPTY config            → ok, no errors, not unavailable (an empty config is VALID).
  * MALFORMED JSON          → not-ok, an ``invalid JSON`` error, NOT unavailable.
  * schema VIOLATION        → not-ok, a {path,message} per error, NOT unavailable.
  * validator FAILURE       → not-ok AND unavailable (never a false ok:True).

Plus the ``/validate`` loopback endpoint (token/Host guard, defined shape, malformed
body) and the per-kind help data.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from rebar.llm.workflow import editor
from rebar.llm.workflow.schema import dump_workflow

# ── validate_node_config: the four DISTINCT axes ────────────────────────────────


def test_empty_config_is_valid_not_malformed():
    # An EMPTY config (empty / whitespace) is VALID — distinct from malformed JSON.
    for text in ("", "   ", "\n\t "):
        res = editor.validate_node_config("scripted", "gate", text)
        assert res == {"ok": True, "errors": [], "unavailable": False}


def test_malformed_json_is_an_error_not_unavailable():
    # MALFORMED/unparseable JSON is a REAL, user-fixable error (NOT unavailable).
    res = editor.validate_node_config("scripted", "gate", "{not valid json")
    assert res["ok"] is False
    assert res["unavailable"] is False
    assert len(res["errors"]) == 1
    assert res["errors"][0]["path"] == ""
    assert res["errors"][0]["message"].startswith("invalid JSON:")


def test_malformed_vs_empty_are_distinct():
    # Explicitly contrast the two: empty → ok:True; malformed → ok:False + invalid JSON.
    empty = editor.validate_node_config("scripted", "gate", "")
    malformed = editor.validate_node_config("scripted", "gate", "{")
    assert empty["ok"] is True and empty["errors"] == []
    assert malformed["ok"] is False
    assert "invalid JSON" in malformed["errors"][0]["message"]
    assert empty["unavailable"] is False and malformed["unavailable"] is False


def test_schema_violation_reports_path_and_message():
    # A schema-VIOLATING `with` (gate with an out-of-enum policy) → ok:False, a located
    # error, NOT unavailable.
    res = editor.validate_node_config("scripted", "gate", '{"with": {"policy": "bogus"}}')
    assert res["ok"] is False
    assert res["unavailable"] is False
    assert res["errors"], "a violation must report at least one error"
    err = res["errors"][0]
    assert "path" in err and "message" in err
    assert "policy" in err["path"]
    assert "bogus" in err["message"]


def test_valid_config_is_ok():
    res = editor.validate_node_config("scripted", "gate", '{"with": {"policy": "strict"}}')
    assert res == {"ok": True, "errors": [], "unavailable": False}


def test_contract_less_node_is_ok():
    # A node whose action has NO declared contract has nothing to validate → ok:True.
    res = editor.validate_node_config("scripted", "no-such-op", '{"with": {"x": 1}}')
    assert res == {"ok": True, "errors": [], "unavailable": False}
    # A control node (branch/loop/map) likewise has no input contract → ok:True.
    assert editor.validate_node_config("branch", None, '{"when": "x"}')["ok"] is True


def test_validator_failure_is_unavailable_never_false_ok(monkeypatch):
    # When the validator ITSELF errors (a non-ValidationError — unresolvable $ref /
    # unknown schema / any crash), the result is the DISTINCT "unavailable" state:
    # ok:False AND unavailable:True — never a false ok:True, never a silent swallow.
    from rebar import schemas

    def _boom(name):
        raise RuntimeError("unresolvable $ref / schema build failed")

    monkeypatch.setattr(schemas, "validator", _boom)
    res = editor.validate_node_config("scripted", "gate", '{"with": {"policy": "strict"}}')
    assert res["ok"] is False
    assert res["unavailable"] is True
    assert res["ok"] is not True  # explicit: NOT a false-valid
    assert res["errors"][0]["message"].startswith("validation unavailable:")


def test_defined_response_shape_keys():
    # Every path returns EXACTLY the defined shape: ok(bool), errors(list of
    # {path,message}), unavailable(bool).
    for res in (
        editor.validate_node_config("scripted", "gate", ""),
        editor.validate_node_config("scripted", "gate", "{"),
        editor.validate_node_config("scripted", "gate", '{"with": {"policy": "bogus"}}'),
    ):
        assert set(res.keys()) == {"ok", "errors", "unavailable"}
        assert isinstance(res["ok"], bool) and isinstance(res["unavailable"], bool)
        for e in res["errors"]:
            assert set(e.keys()) == {"path", "message"}


# ── node_kind_help: the per-kind help DATA ──────────────────────────────────────


def test_node_kind_help_has_all_five_kinds_with_shapes():
    help_data = editor.node_kind_help()
    assert set(help_data.keys()) == {"scripted", "agent", "branch", "loop", "map"}
    for kind, info in help_data.items():
        assert info.get("summary"), f"{kind} must describe its expected shape"
        assert isinstance(info.get("shape"), dict) and info["shape"], f"{kind} needs a shape"


def test_node_kind_help_is_a_copy():
    # Callers cannot mutate the canonical map.
    a = editor.node_kind_help()
    a["scripted"]["title"] = "MUTATED"
    assert editor.node_kind_help()["scripted"]["title"] != "MUTATED"


# ── The /validate loopback endpoint ─────────────────────────────────────────────


def _wf_file(tmp_path: Path) -> Path:
    doc = {
        "schema_version": "2",
        "name": "demo",
        "inputs": {"items": {"type": "array"}},
        "steps": [{"id": "start", "uses": "noop"}],
    }
    p = tmp_path / "demo.yaml"
    p.write_text(dump_workflow(doc), encoding="utf-8")
    return p


@pytest.fixture
def _server(tmp_path):
    path = _wf_file(tmp_path)
    server, host, port, token = editor.edit_workflow(
        path, open_browser=False, serve_forever=False, host="127.0.0.1"
    )
    yield path, f"http://{host}:{port}", token
    server.shutdown()
    server.server_close()


def _validate(base, token, payload, *, with_token=True):
    headers = {"Content-Type": "application/json"}
    if with_token:
        headers["X-Rebar-Token"] = token
    req = urllib.request.Request(
        base + "/validate",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    return urllib.request.urlopen(req)


@pytest.mark.allow_network  # loopback only (127.0.0.1)
def test_validate_endpoint_happy_valid_and_invalid(_server):
    _path, base, token = _server
    ok = json.loads(
        _validate(
            base,
            token,
            {"kind": "scripted", "action": "gate", "config": '{"with": {"policy": "strict"}}'},
        ).read()
    )
    assert ok == {"ok": True, "errors": [], "unavailable": False}
    bad = json.loads(
        _validate(
            base,
            token,
            {"kind": "scripted", "action": "gate", "config": '{"with": {"policy": "bogus"}}'},
        ).read()
    )
    assert bad["ok"] is False and bad["unavailable"] is False
    assert set(bad.keys()) == {"ok", "errors", "unavailable"}
    assert bad["errors"] and "policy" in bad["errors"][0]["path"]


@pytest.mark.allow_network  # loopback only
def test_validate_endpoint_empty_and_malformed(_server):
    _path, base, token = _server
    empty = json.loads(
        _validate(base, token, {"kind": "scripted", "action": "gate", "config": ""}).read()
    )
    assert empty["ok"] is True and empty["errors"] == []
    malformed = json.loads(
        _validate(base, token, {"kind": "scripted", "action": "gate", "config": "{bad"}).read()
    )
    assert malformed["ok"] is False and malformed["unavailable"] is False
    assert "invalid JSON" in malformed["errors"][0]["message"]


@pytest.mark.allow_network  # loopback only
def test_validate_endpoint_rejects_unauthenticated(_server):
    # The token/Host guard rejects a POST without the per-session token.
    _path, base, token = _server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _validate(
            base, token, {"kind": "scripted", "action": "gate", "config": ""}, with_token=False
        )
    assert exc.value.code == 403


@pytest.mark.allow_network  # loopback only
def test_validate_endpoint_malformed_body_is_500_unavailable(_server):
    # A malformed JSON *request body* (not a config string) makes the handler itself
    # raise → HTTP 500 with the defined shape + unavailable:true (client maps 500 →
    # the "validation unavailable" state).
    _path, base, token = _server
    req = urllib.request.Request(
        base + "/validate",
        data=b"{not json at all",
        method="POST",
        headers={"X-Rebar-Token": token, "Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 500
    body = json.loads(exc.value.read())
    assert body["ok"] is False and body["unavailable"] is True
    assert body["errors"][0]["message"].startswith("validation endpoint error:")


@pytest.mark.allow_network  # loopback only
def test_help_endpoint_serves_kind_help(_server):
    _path, base, _token = _server
    data = json.loads(urllib.request.urlopen(base + "/help").read())
    assert set(data.keys()) == {"scripted", "agent", "branch", "loop", "map"}
