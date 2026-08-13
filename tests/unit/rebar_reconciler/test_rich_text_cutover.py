"""Rich-text cutover flag + codec repointing (story 3388, epic 708d).

Stories ``e59d`` and ``271c`` landed the Markdown-aware Cloud ADF functions and the DC
segmenting wiki renderer as PURE capability, wired to nothing. This story makes them
reachable on the live send paths — behind ``reconciler.rich_text_cutover``, which ships
``off``.

What these tests pin:

* **Default OFF.** With no configuration, every codec op is byte-identical to the
  historical plain/identity wire. The existing ``test_rich_text_codec.py`` suite passes
  UNMODIFIED for the same reason.
* **Rollback is the flag.** Setting the flag back to ``off`` restores the plain wire —
  no capability revert, no redeploy.
* **The codec law survives the cutover.** In rich mode
  ``normalize_outbound(t) == decode_inbound(to_wire(t))`` holds BY CONSTRUCTION, because
  ``normalize_outbound`` is derived from ``to_wire`` rather than written twice.
* **Both DC send paths render.** The update path (via ``OutboundFieldMapper``) and the
  CREATE path (``_issues.py``) must agree; a create that fits without rendering would
  post raw Markdown and then rendered wiki on the next update.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rebar_reconciler.adapters.jira.rich_text_codec import AdfCodec
from rebar_reconciler.adapters.jira_family import rich_text
from rebar_reconciler.adapters.jira_family.rich_text import WikiTextCodec, cutover_clients

_MD = "# Heading\n\n- alpha\n- beta **bold**\n"


class _Cfg:
    """Minimal stand-in for the typed config's reconciler section."""

    def __init__(self, value: str) -> None:
        self.reconciler = type("R", (), {"rich_text_cutover": value})()


@pytest.fixture
def set_flag(monkeypatch: pytest.MonkeyPatch):
    """Set ``reconciler.rich_text_cutover`` as the resolver will read it."""

    def _set(value: str) -> None:
        import rebar.config

        monkeypatch.setattr(rebar.config, "load_config", lambda *a, **k: _Cfg(value))

    return _set


# ---------------------------------------------------------------------------
# The flag itself
# ---------------------------------------------------------------------------


def test_cutover_defaults_off(set_flag: Any) -> None:
    """Ships OFF: opt-in per client, never a 100%-traffic flip."""
    set_flag("off")

    assert cutover_clients() == frozenset()
    assert WikiTextCodec(rich="dc" in cutover_clients()).to_wire(_MD) == _MD
    assert AdfCodec(rich="cloud" in cutover_clients()).to_wire(_MD) == __import__(
        "rebar_reconciler.adapters.jira.adf", fromlist=["adf"]
    ).text_to_adf(_MD)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("off", frozenset()),
        ("cloud", frozenset({"cloud"})),
        ("dc", frozenset({"dc"})),
        ("both", frozenset({"cloud", "dc"})),
    ],
)
def test_flag_selects_clients(set_flag: Any, value: str, expected: frozenset[str]) -> None:
    set_flag(value)

    assert cutover_clients() == expected


def test_flag_fails_closed_when_config_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A config problem must never silently cut a client over."""
    import rebar.config

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise rebar.config.ConfigError("broken")

    monkeypatch.setattr(rebar.config, "load_config", _boom)

    assert cutover_clients() == frozenset()


def test_flag_is_read_at_call_time_not_import_time(set_flag: Any) -> None:
    """A long-lived reconciler must see a flip without a redeploy."""
    set_flag("off")
    assert cutover_clients() == frozenset()

    set_flag("both")
    assert cutover_clients() == frozenset({"cloud", "dc"})


# ---------------------------------------------------------------------------
# Cutover behaviour
# ---------------------------------------------------------------------------


def test_dc_cutover_sends_rendered_wiki() -> None:
    """After the DC cutover the wire is wiki markup, not raw Markdown."""
    wire = WikiTextCodec(rich=True).to_wire(_MD)

    assert "h1. Heading" in wire
    assert "* alpha" in wire
    assert "# Heading" not in wire


def test_cloud_cutover_sends_structured_adf() -> None:
    """After the Cloud cutover the wire carries real ADF nodes, not literal Markdown."""
    doc = AdfCodec(rich=True).to_wire(_MD)

    assert doc["type"] == "doc"
    kinds = {node.get("type") for node in doc["content"]}
    assert "heading" in kinds
    assert kinds != {"paragraph"}


def test_cutover_rollback_restores_plain() -> None:
    """Flipping back to plain restores the historical wire exactly."""
    from rebar_reconciler.adapters.jira import adf

    assert WikiTextCodec(rich=False).to_wire(_MD) == _MD
    assert WikiTextCodec(rich=False).normalize_outbound(_MD) == _MD
    assert AdfCodec(rich=False).to_wire(_MD) == adf.text_to_adf(_MD)
    assert AdfCodec(rich=False).normalize_outbound(_MD) == adf.normalize_description(_MD)


@pytest.mark.parametrize("rich", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        "a single short line",
        "one\ntwo\nthree",
        "# Heading\n\n- alpha\n",
        "text with `code` and **bold**",
    ],
)
def test_codec_law_holds_in_both_modes(rich: bool, body: str) -> None:
    """``normalize_outbound(t) == decode_inbound(to_wire(t))`` — ticket a32a.

    The law is what the outbound comment differ's dedup key depends on, and the whole
    point of deriving ``normalize_outbound`` from ``to_wire`` in rich mode is that the
    two cannot drift apart.
    """
    for codec in (WikiTextCodec(rich=rich), AdfCodec(rich=rich)):
        assert codec.normalize_outbound(body) == codec.decode_inbound(codec.to_wire(body))


def test_non_string_values_pass_through_in_rich_mode() -> None:
    """The mapper's "never coerce" behaviour survives the cutover."""
    sentinel = {"already": "shaped"}

    assert WikiTextCodec(rich=True).fit_outbound(sentinel) is sentinel  # type: ignore[arg-type]
    assert WikiTextCodec(rich=True).normalize_outbound(sentinel) is sentinel  # type: ignore[arg-type]
    assert WikiTextCodec(rich=True).to_wire(sentinel) is sentinel  # type: ignore[arg-type]


def test_dc_create_path_renders_like_the_update_path(set_flag: Any) -> None:
    """BOTH DC send sites must render, or create posts raw Markdown.

    ``_issues.py`` previously fitted the description WITHOUT ``to_wire``, so after a
    cutover a created issue would carry raw Markdown while every later update carried
    rendered wiki.
    """
    from rebar_reconciler.adapters.jira_datacenter import _issues

    set_flag("dc")
    fields = _issues._translate_create_fields({"title": "headline", "description": _MD})

    codec = WikiTextCodec(rich=True)
    assert fields["description"] == codec.to_wire(codec.fit_outbound(_MD))
    assert "h1. Heading" in fields["description"]


def test_dc_backend_create_path_renders_like_the_update_path(set_flag: Any) -> None:
    """The SECOND DC create path, which the test above does not reach.

    ``_map_local_to_dc_fields`` is ``map_ticket_to_remote``'s implementation — a create
    path entirely separate from ``_issues._translate_create_fields``. It built a rich
    codec and then applied only ``fit_outbound``, so with the DC cutover ON a created
    issue carried raw Markdown while every later update carried rendered wiki: the
    formatting looked broken until somebody edited the issue.

    Having two independent DC create paths is exactly why this is asserted twice. The
    sibling test passing said nothing about this one.
    """
    from rebar_reconciler.adapters.jira_datacenter import backend

    set_flag("dc")
    fields = backend._map_local_to_dc_fields({"title": "headline", "description": _MD})

    codec = WikiTextCodec(rich=True)
    assert fields["description"] == codec.to_wire(codec.fit_outbound(_MD))
    assert "h1. Heading" in fields["description"]
    assert "# Heading" not in fields["description"]


def test_dc_backend_create_path_is_unchanged_when_flag_is_off(set_flag: Any) -> None:
    """Flag off keeps the plain wire on this path too — the rollback must cover both."""
    from rebar_reconciler.adapters.jira_datacenter import backend

    set_flag("off")
    fields = backend._map_local_to_dc_fields({"title": "headline", "description": _MD})
    assert fields["description"] == _MD


def test_dc_create_path_is_unchanged_when_flag_is_off(set_flag: Any) -> None:
    from rebar_reconciler.adapters.jira_datacenter import _issues

    set_flag("off")
    fields = _issues._translate_create_fields({"title": "headline", "description": _MD})

    assert fields["description"] == _MD


def test_cloud_functions_degrade_without_marklas(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC from e59d, inherited here: a no-extras install must still cut over safely.

    With the ``adf`` extra absent the Markdown-aware functions return their plain
    results, so the cutover path emits the plain-text ADF wire and raises nothing.
    """
    from rebar_reconciler.adapters.jira import adf

    monkeypatch.setattr(adf, "_marklas", lambda: None)

    assert AdfCodec(rich=True).to_wire(_MD) == adf.text_to_adf(_MD)
    assert AdfCodec(rich=True).fit_outbound(_MD) == _MD


def test_dc_degrades_without_pandoc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same for DC: no pandoc means the identity wire, never an exception."""
    from rebar_reconciler.adapters.jira_family import wiki_render

    monkeypatch.setattr(wiki_render, "_pandoc_path", lambda: None)

    assert WikiTextCodec(rich=True).to_wire(_MD) == _MD


def test_resolver_lives_in_the_shared_layer_without_cloud_imports() -> None:
    """ADR 0083: ``jira_family`` must not import ``adapters/jira/``.

    Checked over the parsed IMPORT statements, not the file text — the module discusses
    ``adapters.jira.comment_limits`` in a comment (deliberately, to explain why the DC
    limit matches Cloud's), and a substring search would flag that prose as a violation.
    """
    import ast

    source = (rich_text.__file__ or "").replace(".pyc", ".py")
    tree = ast.parse(Path(source).read_text(encoding="utf-8"))

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)

    offenders = [
        m
        for m in imported
        if m.startswith("rebar_reconciler.adapters.jira.") or m == "rebar_reconciler.adapters.jira"
    ]
    assert offenders == []
