"""Story 6592: the prompt-authoring backbone — list, write-target detection, and the
atomic save (round-trip golden + every non-happy path) + the editor's prompt endpoints.

The Python here is the verification floor: a created/edited prompt must round-trip
BYTE-FOR-BYTE (no corruption), the write target must be auto-detected (packaged source
checkout vs project override vs neither), and every non-happy path (invalid id, id
collision, neither-writable) must raise a clear typed error WITHOUT corrupting any
existing file.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from rebar.llm.prompts import get_prompt, parse_front_matter
from rebar.llm.workflow import editor
from rebar.llm.workflow.prompt_authoring import (
    PromptWriteError,
    list_prompts,
    prompt_write_target,
    save_prompt,
)

# ── round-trip golden: save_prompt then get_prompt is byte-for-byte ─────────────


def _project_repo(tmp_path: Path) -> Path:
    (tmp_path / ".rebar" / "prompts").mkdir(parents=True)
    return tmp_path


def test_save_then_get_round_trips_byte_for_byte_create(tmp_path):
    repo = _project_repo(tmp_path)
    body = "Do the thing for {{ticket_id}}.\n\n- step one\n- step two"
    meta = {"title": "Summarize", "category": "transform", "description": "desc"}
    out = save_prompt("my-summary", meta, body, repo_root=str(repo))
    assert out["kind"] == "project"
    # The resolved prompt body is the SAME bytes we wrote (write_front_matter preserves
    # the body byte-for-byte after the closing fence).
    assert get_prompt("my-summary", repo_root=str(repo)).text == body


def test_save_then_get_round_trips_byte_for_byte_edit(tmp_path):
    repo = _project_repo(tmp_path)
    save_prompt("editme", {"category": "transform"}, "original body", repo_root=str(repo))
    new_body = "EDITED body\nwith a trailing line and no newline"
    out = save_prompt(
        "editme", {"category": "transform"}, new_body, repo_root=str(repo), overwrite=True
    )
    assert out["kind"] == "project"
    assert get_prompt("editme", repo_root=str(repo)).text == new_body


# ── non-happy paths ─────────────────────────────────────────────────────────────


def test_invalid_id_refused(tmp_path):
    repo = _project_repo(tmp_path)
    for bad in ("", "-leading", "Has Space", "UPPER", "has/slash", "..", "a.b"):
        with pytest.raises(PromptWriteError, match="invalid prompt id"):
            save_prompt(bad, {}, "body", repo_root=str(repo))


def test_collision_without_overwrite_refused_then_overwrite_succeeds(tmp_path):
    repo = _project_repo(tmp_path)
    save_prompt("dup", {"category": "transform"}, "first", repo_root=str(repo))
    with pytest.raises(PromptWriteError, match="already exists"):
        save_prompt("dup", {"category": "transform"}, "second", repo_root=str(repo))
    # The original is intact (no silent corruption from the refused write).
    assert get_prompt("dup", repo_root=str(repo)).text == "first"
    # With overwrite the new body wins.
    save_prompt("dup", {"category": "transform"}, "second", repo_root=str(repo), overwrite=True)
    assert get_prompt("dup", repo_root=str(repo)).text == "second"


def test_collision_against_builtin_id_refused(tmp_path):
    # Creating a new prompt whose id shadows a built-in is a collision too.
    repo = _project_repo(tmp_path)
    with pytest.raises(PromptWriteError, match="already exists"):
        save_prompt("ticket-quality", {"category": "review"}, "body", repo_root=str(repo))


def test_neither_writable_refused(tmp_path, monkeypatch):
    # No repo_root → no project dir AND not a nava-rebar checkout → kind "none".
    repo = tmp_path / "plain"
    repo.mkdir()
    # Make the project prompt dir non-creatable by pointing at a read-only ancestor.
    ro = repo / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        target = prompt_write_target("x", repo_root=str(ro))
        assert target["kind"] == "none" and target["writable"] is False
        with pytest.raises(PromptWriteError, match="neither location is writable"):
            save_prompt("x", {}, "body", repo_root=str(ro))
    finally:
        ro.chmod(0o700)


# ── prompt_write_target detection ───────────────────────────────────────────────


def _fake_nava_checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "nava-rebar"\nversion = "0"\n', encoding="utf-8"
    )
    return repo


def test_target_packaged_for_nava_rebar_checkout(tmp_path, monkeypatch):
    repo = _fake_nava_checkout(tmp_path)
    # A writable fake packaged reviewers dir.
    pkg = tmp_path / "reviewers"
    pkg.mkdir()
    monkeypatch.setattr("rebar.llm.workflow.prompt_authoring._packaged_dir", lambda: pkg)
    target = prompt_write_target("new-builtin", repo_root=str(repo))
    assert target["kind"] == "packaged"
    assert target["path"].endswith("new_builtin.md")  # underscores in the packaged file


def test_target_project_for_plain_dir_with_writable_rebar(tmp_path):
    repo = _project_repo(tmp_path)
    target = prompt_write_target("foo", repo_root=str(repo))
    assert target["kind"] == "project"
    assert target["path"].endswith("/.rebar/prompts/foo.md")


def test_target_none_when_neither(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        assert prompt_write_target("foo", repo_root=str(ro))["kind"] == "none"
    finally:
        ro.chmod(0o700)


# ── list_prompts: built-ins + project + override marking ─────────────────────────


def test_list_prompts_built_ins_and_project_and_override(tmp_path):
    repo = _project_repo(tmp_path)
    # A brand-new project prompt and an override of a built-in id.
    save_prompt("proj-only", {"category": "transform", "title": "P"}, "b", repo_root=str(repo))
    pdir = repo / ".rebar" / "prompts"
    (pdir / "ticket-quality.md").write_text(
        "---\ncategory: review\ndimension: ticket-quality\n---\nOVERRIDE", encoding="utf-8"
    )
    rows = list_prompts(repo_root=str(repo))
    by_id = {r["id"]: r for r in rows}
    assert "ticket-quality" in by_id and "proj-only" in by_id
    # A built-in with no project override stays source=builtin.
    assert by_id["security"]["source"] == "builtin"
    # The project-only prompt and the override are both source=project.
    assert by_id["proj-only"]["source"] == "project"
    assert by_id["ticket-quality"]["source"] == "project"  # override wins, appears ONCE
    assert sum(1 for r in rows if r["id"] == "ticket-quality") == 1
    # Rows carry the grouping fields.
    assert set(by_id["proj-only"]) >= {
        "id",
        "title",
        "category",
        "is_reviewer",
        "source",
        "inputs",
        "outputs",
        "description",
    }


# ── index regen on a packaged write ─────────────────────────────────────────────


def test_packaged_save_regenerates_index(tmp_path, monkeypatch):
    repo = _fake_nava_checkout(tmp_path)
    pkg = tmp_path / "reviewers"
    pkg.mkdir()
    # Seed a single existing reviewer so the index invariant (exactly one default) holds.
    (pkg / "keep.md").write_text(
        "---\ncategory: review\ndimension: dk\ndefault: true\n---\nKEEP", encoding="utf-8"
    )
    monkeypatch.setattr("rebar.llm.workflow.prompt_authoring._packaged_dir", lambda: pkg)
    monkeypatch.setattr("rebar.llm.prompts._catalog_dir", lambda: pkg)
    out = save_prompt(
        "extra-review",
        {"category": "review", "dimension": "dx", "default": False},
        "an extra reviewer",
        repo_root=str(repo),
    )
    assert out["kind"] == "packaged" and out["regenerated_index"] is True
    index = json.loads((pkg / "index.json").read_text(encoding="utf-8"))
    assert "extra-review" in index and "keep" in index  # the new id is in the derived index


# ── editor endpoints (loopback server, no browser) ──────────────────────────────


@pytest.fixture
def _server(tmp_path):
    # A workflow file inside a project repo whose .rebar/prompts is writable, so the
    # endpoints resolve a "project" write target.
    from rebar.llm.workflow.schema import dump_workflow

    repo = _project_repo(tmp_path)
    save_prompt(
        "lib-one", {"category": "transform", "title": "One"}, "body one", repo_root=str(repo)
    )
    wf = repo / "demo.yaml"
    wf.write_text(
        dump_workflow({"schema_version": "2", "name": "d", "steps": [{"id": "a", "uses": "noop"}]}),
        encoding="utf-8",
    )
    import rebar.config as _cfg

    # Pin repo_root to our project so the handler lists/writes against it.
    orig = _cfg.repo_root
    _cfg.repo_root = lambda: repo  # type: ignore[assignment]
    server, host, port, token = editor.edit_workflow(
        wf, open_browser=False, serve_forever=False, host="127.0.0.1"
    )
    try:
        yield repo, f"http://{host}:{port}", token
    finally:
        _cfg.repo_root = orig  # type: ignore[assignment]
        server.shutdown()
        server.server_close()


def _get(base, token, path):
    req = urllib.request.Request(base + path, headers={"X-Rebar-Token": token})
    return urllib.request.urlopen(req)


def _post_json(base, token, path, obj, with_token=True):
    headers = {"Content-Type": "application/json"}
    if with_token:
        headers["X-Rebar-Token"] = token
    req = urllib.request.Request(
        base + path, data=json.dumps(obj).encode("utf-8"), method="POST", headers=headers
    )
    return urllib.request.urlopen(req)


@pytest.mark.allow_network  # loopback only
def test_endpoint_list_prompts(_server):
    _repo, base, token = _server
    rows = json.loads(_get(base, token, "/prompts").read())
    ids = {r["id"] for r in rows}
    assert "lib-one" in ids and "ticket-quality" in ids


@pytest.mark.allow_network  # loopback only
def test_endpoint_get_prompt_shows_target(_server):
    _repo, base, token = _server
    data = json.loads(_get(base, token, "/prompt?id=lib-one").read())
    assert data["id"] == "lib-one" and data["text"] == "body one"
    assert data["meta"]["category"] == "transform"
    assert data["target"]["kind"] == "project"  # resolved write target shown before save


@pytest.mark.allow_network  # loopback only
def test_endpoint_get_prompt_unknown_404(_server):
    _repo, base, token = _server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base, token, "/prompt?id=nope-nope")
    assert exc.value.code == 404
    assert json.loads(exc.value.read())["errors"]


@pytest.mark.allow_network  # loopback only
def test_endpoint_prompt_save_happy(_server):
    repo, base, token = _server
    resp = _post_json(
        base,
        token,
        "/prompt/save",
        {"id": "via-http", "meta": {"category": "transform"}, "body": "hi"},
    )
    assert resp.status == 200
    out = json.loads(resp.read())
    assert out["ok"] is True and out["kind"] == "project"
    assert (repo / ".rebar" / "prompts" / "via-http.md").is_file()


@pytest.mark.allow_network  # loopback only
def test_endpoint_prompt_save_collision_4xx(_server):
    _repo, base, token = _server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(base, token, "/prompt/save", {"id": "lib-one", "meta": {}, "body": "x"})
    assert exc.value.code == 400
    assert any("already exists" in e for e in json.loads(exc.value.read())["errors"])


@pytest.mark.allow_network  # loopback only
def test_endpoint_prompt_save_rejects_without_token(_server):
    repo, base, _token = _server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(
            base,
            _token,
            "/prompt/save",
            {"id": "noauth", "meta": {}, "body": "x"},
            with_token=False,
        )
    assert exc.value.code == 403
    assert not (repo / ".rebar" / "prompts" / "noauth.md").exists()  # no write


@pytest.mark.allow_network  # loopback only
def test_endpoint_prompts_rejects_without_token(_server):
    _repo, base, _token = _server
    req = urllib.request.Request(base + "/prompts")  # no token
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 403


def test_parse_front_matter_smoke():
    # Sanity: a saved file's front-matter parses back (used by the /prompt endpoint).
    meta, body = parse_front_matter("---\ncategory: transform\n---\nbody")
    assert meta["category"] == "transform" and body == "body"
