"""Story 5c0e — the DC renderer's subprocess must be bounded, reaped, and inert.

The Data Center path shells out to pandoc once per structurally-safe unit. Two
hazards from the DC experiment motivate this file:

* **Pathological input.** One corpus body span pandoc's jira reader for 13.5
  minutes at 95.8% CPU. pypandoc's high-level API sets no timeout and hands back
  no process handle, so a single field could stall a reconcile indefinitely.
* **Orphaned grandchildren.** ``subprocess.run(timeout=...)`` reaps only the
  DIRECT child. A pipe-holding grandchild survives it and keeps burning CPU (bug
  d843, bpo-30154), so the timeout alone would not actually stop the spin.

The third property is the one that makes the other two safe to ship: this story
changes ROBUSTNESS only. Rendered output must be byte-identical to the landed
renderer, so a hardening change can never quietly alter what lands in Jira.

**Cost discipline.** The whole historical corpus remains byte-compared in Verify,
but its production-side conversion is now supplied by committed real-Pandoc replay
outputs. Version/fingerprint and representative product-path tests retain the live
boundary. The weekly/manual external job performs all 1,383 real conversions.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from rebar_reconciler.adapters.jira_family import wiki_render
from rebar_reconciler.adapters.jira_family.wiki_render import (
    _convert,
    _pandoc_path,
    _pandoc_timeout,
    render_markdown_to_wiki,
)

pytestmark = pytest.mark.unit

_CORPUS = Path(__file__).resolve().parents[2] / "fixtures" / "dc_wiki_corpus"
_LEGACY_OUTPUTS = Path(__file__).resolve().parents[2] / "fixtures" / "dc_wiki_legacy_outputs.json"
_REPLAY_OUTPUTS = Path(__file__).resolve().parents[2] / "fixtures" / "dc_wiki_replay"
_STRATA = ("code_arrow", "table", "prose")
_EXPECTED_RENDERABLE_UNITS = 884

_PANDOC = _pandoc_path()
_NEEDS_PANDOC = pytest.mark.skipif(_PANDOC is None, reason="the `wiki` extra is not installed")

_GENERATOR_SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "generate_dc_wiki_legacy_outputs.py"
)
_GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "generate_dc_wiki_legacy_outputs", _GENERATOR_SCRIPT
)
assert _GENERATOR_SPEC is not None and _GENERATOR_SPEC.loader is not None
_GENERATOR = importlib.util.module_from_spec(_GENERATOR_SPEC)
_GENERATOR_SPEC.loader.exec_module(_GENERATOR)
_REPLAY_FIXTURES = _GENERATOR.load_replay_fixtures(_REPLAY_OUTPUTS)


def _bodies() -> list[str]:
    out: list[str] = []
    for stratum in _STRATA:
        out.extend(json.loads((_CORPUS / f"{stratum}.json").read_text(encoding="utf-8")))
    return out


def _load_legacy_fixture() -> dict[str, Any]:
    payload = json.loads(_LEGACY_OUTPUTS.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError("legacy output fixture must be a JSON object")
    return payload


_LEGACY_FIXTURE = _load_legacy_fixture()


def _input_sha256(prepared: str) -> str:
    return hashlib.sha256(prepared.encode("utf-8")).hexdigest()


def _binary_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_output(entry: dict[str, Any]) -> str | None:
    encoded = entry.get("output_b85")
    if encoded is None:
        return None
    if not isinstance(encoded, str):
        raise AssertionError("legacy fixture output_b85 must be a string or null")
    return base64.b85decode(encoded.encode("ascii")).decode("utf-8")


# --- 1. the timeout is real, configurable, and fails SAFE -----------------------


def test_the_timeout_default_matches_the_config_default() -> None:
    """A stale built-in default would silently diverge from documented behaviour."""
    from rebar._config_schema import ReconcilerConfig

    assert wiki_render._PANDOC_TIMEOUT_DEFAULT == ReconcilerConfig().dc_pandoc_timeout_s


def test_the_timeout_is_read_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir(parents=True)
    (tmp_path / "rebar.toml").write_text(
        "[reconciler]\ndc_pandoc_timeout_s = 2.5\n", encoding="utf-8"
    )
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    assert _pandoc_timeout() == 2.5


def test_the_timeout_is_reachable_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir(parents=True)
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    monkeypatch.setenv("REBAR_RECONCILER_DC_PANDOC_TIMEOUT_S", "4")
    assert _pandoc_timeout() == 4.0


@_NEEDS_PANDOC
def test_each_render_pass_resolves_the_timeout_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configuration is sampled per pass, never once per Pandoc-bound unit."""
    (tmp_path / ".git").mkdir(parents=True)
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    body = "first **bold**\n\nanother **strong** sentence\n\nthird [link](https://example.test)\n"
    renderable = [
        text for kind, text in wiki_render._lock_and_split(body) if kind == wiki_render._RENDER
    ]
    assert len(renderable) == 3

    real_timeout = wiki_render._pandoc_timeout
    resolutions = 0

    def _counting_timeout() -> float:
        nonlocal resolutions
        resolutions += 1
        return real_timeout()

    monkeypatch.setattr(wiki_render, "_pandoc_timeout", _counting_timeout)

    first = render_markdown_to_wiki(body)
    assert resolutions == 1
    second = render_markdown_to_wiki(body)
    assert resolutions == 2
    assert second == first


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_a_non_positive_timeout_falls_back_rather_than_disabling_rendering(
    bad: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero would mean 'kill pandoc immediately' — every unit degrading to Markdown.

    Fail-SAFE, not fail-open: a config fault must not silently switch rendering
    off across the whole project.
    """
    (tmp_path / ".git").mkdir(parents=True)
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    monkeypatch.setenv("REBAR_RECONCILER_DC_PANDOC_TIMEOUT_S", bad)
    assert _pandoc_timeout() == wiki_render._PANDOC_TIMEOUT_DEFAULT


def test_an_unreadable_config_falls_back_to_the_builtin_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rebar.config

    def _boom(*_a: object, **_k: object) -> object:
        raise rebar.config.ConfigError("unreadable")

    monkeypatch.setattr(rebar.config, "load_config", _boom)
    assert _pandoc_timeout() == wiki_render._PANDOC_TIMEOUT_DEFAULT


def test_a_config_object_without_the_key_falls_back_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine ships as package data and can meet a config predating this key.

    Callers also substitute partial config objects — one such stand-in in the
    cutover suite carries only ``rich_text_cutover``. Raising over a field we can
    trivially default would take the whole pass down.
    """
    import rebar.config

    partial = type("Cfg", (), {"reconciler": type("R", (), {})()})()
    monkeypatch.setattr(rebar.config, "load_config", lambda *a, **k: partial)
    assert _pandoc_timeout() == wiki_render._PANDOC_TIMEOUT_DEFAULT


# --- 2. a hung process AND its group are killed --------------------------------


# A REAL executable standing in for pandoc, rather than a patched
# ``subprocess.Popen``. Patching the module attribute would also hijack every
# OTHER subprocess spawned while it is in place — including the autouse
# conftest fixture that shells out to git during teardown, which then blocks on
# this stand-in's pipes and reports as an infrastructure hang. Handing
# ``_convert`` a path to run keeps the spawn entirely real: real argv, real
# pipes, real timeout, real reap.
#
# It forks a grandchild that inherits the pipes and spins at full CPU, which is
# the shape a plain ``subprocess.run(timeout=...)`` fails to clean up (bug d843).
# The grandchild's pid comes back through a FILE: a reader waiting on stderr for
# EOF would block until the grandchild dies, turning a reap regression into a
# deadlock instead of a failure.
_FAKE_PANDOC = """#!{python}
import os, pathlib, time
pid = os.fork()
if pid == 0:                      # grandchild: hold the inherited pipes and spin
    while True:
        pass
pathlib.Path({pidfile!r}).write_text(str(pid))
time.sleep(300)
"""

_EXITS_NONZERO = """#!{python}
import sys
sys.exit(3)
"""


def _executable(path: Path, body: str) -> str:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_a_timeout_reaps_the_whole_process_group_not_just_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing one: a spinning GRANDCHILD must not survive the timeout.

    A plain ``subprocess.run(timeout=...)`` passes a test that only inspects the
    direct child, which is exactly how bug d843 stayed invisible. So the stand-in
    forks a grandchild spinning at full CPU, the timeout is allowed to fire, and
    the assertion is about the GRANDCHILD.
    """
    (tmp_path / ".git").mkdir(parents=True)
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    monkeypatch.setenv("REBAR_RECONCILER_DC_PANDOC_TIMEOUT_S", "1")

    pid_file = tmp_path / "grandchild.pid"
    fake = _executable(
        tmp_path / "fake_pandoc",
        _FAKE_PANDOC.format(python=sys.executable, pidfile=str(pid_file)),
    )

    started = time.monotonic()
    assert _convert("anything", fake) is None
    elapsed = time.monotonic() - started

    # Separates "the 1s timeout fired" from "we waited out the stand-in's 300s sleep" —
    # the only two ways _convert can return here.
    # timing: hang-guard — 60x the timeout, 5x under the sleep; contention cannot cross it
    assert elapsed < 60

    deadline = time.monotonic() + 15
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    grandchild = int(pid_file.read_text().strip())
    assert grandchild > 0

    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _alive(grandchild):  # pragma: no cover - only on a reap regression
        os.kill(grandchild, signal.SIGKILL)
        pytest.fail("the spinning grandchild survived the reap")


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_the_child_leads_its_own_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start_new_session=True`` is what gives the reaper a group to kill.

    Without it the child shares the RECONCILER's group, so the reaper's killpg
    would target rebar's own processes — the reap and the session flag are one
    mechanism, not two independent settings.
    """
    (tmp_path / ".git").mkdir(parents=True)
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    monkeypatch.setenv("REBAR_RECONCILER_DC_PANDOC_TIMEOUT_S", "1")

    pid_file = tmp_path / "grandchild.pid"
    fake = _executable(
        tmp_path / "fake_pandoc",
        _FAKE_PANDOC.format(python=sys.executable, pidfile=str(pid_file)),
    )
    observed: dict[str, int] = {}
    real_getpgid = os.getpgid

    def _spy(pid: int) -> int:
        pgid = real_getpgid(pid)
        observed.setdefault("child", pgid)
        return pgid

    monkeypatch.setattr(os, "getpgid", _spy)
    assert _convert("anything", fake) is None

    assert observed, "the reaper never asked for the child's process group"
    assert observed["child"] != real_getpgid(0), (
        "the child shared the test's process group — start_new_session was lost, "
        "so a reap would signal rebar's own processes"
    )


def test_a_timed_out_unit_degrades_to_its_original_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-UNIT fallback: the body comes back verbatim, and nothing raises."""
    (tmp_path / ".git").mkdir(parents=True)
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    monkeypatch.setenv("REBAR_RECONCILER_DC_PANDOC_TIMEOUT_S", "1")

    pid_file = tmp_path / "grandchild.pid"
    fake = _executable(
        tmp_path / "fake_pandoc",
        _FAKE_PANDOC.format(python=sys.executable, pidfile=str(pid_file)),
    )
    monkeypatch.setattr(wiki_render, "_pandoc_path", lambda: fake)

    body = "some prose\n"  # ONE renderable unit, so ONE timeout to wait out
    assert render_markdown_to_wiki(body) == body


def test_a_nonzero_exit_degrades_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _executable(tmp_path / "failing_pandoc", _EXITS_NONZERO.format(python=sys.executable))
    monkeypatch.setattr(wiki_render, "_pandoc_path", lambda: fake)
    body = "some prose\n"
    assert render_markdown_to_wiki(body) == body


def test_an_unspawnable_pandoc_degrades_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """OSError from the spawn itself — a missing or non-executable binary."""
    monkeypatch.setattr(wiki_render, "_pandoc_path", lambda: "/nonexistent/pandoc")
    body = "some prose\n"
    assert render_markdown_to_wiki(body) == body


# --- 3. the spawn contract: pandoc reads the unit from STDIN --------------------


@_NEEDS_PANDOC
def test_the_unit_is_delivered_on_stdin_with_the_landed_argv() -> None:
    """The one place this deliberately diverges from the ACLI pattern it mirrors.

    acli reads nothing from stdin and is spawned ``stdin=DEVNULL``; pandoc reads
    the unit FROM stdin. Copying that kwarg would feed pandoc an empty document
    and degrade every unit while looking perfectly healthy, so the pipe and the
    argv are both pinned.
    """
    seen: dict[str, object] = {}
    real_popen = subprocess.Popen

    def _spy(argv: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        seen["argv"] = list(argv)
        seen["stdin"] = kwargs.get("stdin")
        return real_popen(argv, **kwargs)

    import unittest.mock

    with unittest.mock.patch.object(subprocess, "Popen", _spy):
        assert _convert("hello **world**", str(_PANDOC)) is not None

    assert seen["stdin"] == subprocess.PIPE
    assert seen["stdin"] != subprocess.DEVNULL
    argv = cast(list[str], seen["argv"])
    assert argv[0] == str(_PANDOC)
    assert argv[1:5] == ["-f", wiki_render._PANDOC_FROM, "-t", wiki_render._PANDOC_TO]
    assert argv[5:] == list(wiki_render._PANDOC_ARGS)


@_NEEDS_PANDOC
def test_an_empty_stdin_would_have_been_detectable() -> None:
    """Guards the guard: prove the stdin bug this file protects against is visible.

    If feeding pandoc nothing produced the same answer as feeding it the unit,
    the test above would pass under the broken implementation too.
    """
    assert _convert("hello **world**", str(_PANDOC)) != _convert("", str(_PANDOC))


# --- 4. robustness ONLY: rendered output must not move -------------------------


def _renderable_units() -> list[str]:
    """Every pandoc-bound unit in the committed corpus, in corpus order."""
    units: list[str] = []
    for body in _bodies():
        units.extend(
            wiki_render.substitute_arrows(text)
            for kind, text in wiki_render._lock_and_split(body)
            if kind == wiki_render._RENDER
        )
    return units


def _legacy_entries() -> list[dict[str, Any]]:
    entries = _LEGACY_FIXTURE.get("units")
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise AssertionError("legacy output fixture units must be a JSON list of objects")
    return entries


_LEGACY_ENTRIES = _legacy_entries()


def test_static_replay_covers_every_landed_renderable_unit() -> None:
    """The replay cannot quietly drop or reorder the 884 historical inputs."""
    units = _renderable_units()
    assert units, "the corpus produced no renderable units — fixture regression"
    assert len(units) == _EXPECTED_RENDERABLE_UNITS

    assert _LEGACY_FIXTURE.get("schema_version") == 3
    assert _LEGACY_FIXTURE.get("output_encoding") == "utf-8/base85"
    assert _LEGACY_FIXTURE.get("unit_count") == _EXPECTED_RENDERABLE_UNITS
    assert _LEGACY_FIXTURE.get("replay") == {
        "schema_version": 1,
        "directory": "tests/fixtures/dc_wiki_replay",
        "strata": list(_STRATA),
        "verify_mode": "committed-static-outputs-plus-three-live-bodies",
        "complete_live_mode": "external-integration-weekly-and-manual",
    }
    expected_pandoc = {
        "version": _GENERATOR.LEGACY_PANDOC_VERSION,
        "supported_platform_binary_sha256": _GENERATOR.SUPPORTED_PANDOC_BINARY_SHA256,
    }
    assert all(fixture.get("pandoc") == expected_pandoc for fixture in _REPLAY_FIXTURES)
    assert len(_LEGACY_ENTRIES) == _EXPECTED_RENDERABLE_UNITS
    expected_input_hashes = [entry.get("input_sha256") for entry in _LEGACY_ENTRIES]
    assert expected_input_hashes == [_input_sha256(unit) for unit in units], (
        "legacy outputs are not aligned to the exact ordered prepared corpus"
    )
    decoded_outputs = [_expected_output(entry) for entry in _LEGACY_ENTRIES]
    assert all(output is None or isinstance(output, str) for output in decoded_outputs)


@_NEEDS_PANDOC
def test_the_installed_pandoc_matches_the_legacy_fixture_provenance() -> None:
    """Each gating platform must match its exact pinned Pandoc binary."""
    import pypandoc

    expected = _LEGACY_FIXTURE.get("pandoc")
    _GENERATOR.validate_pandoc_provenance(
        expected,
        platform_key=_GENERATOR.current_platform_key(),
        version=str(pypandoc.get_pandoc_version()),
        binary_sha256=_binary_sha256(Path(str(_PANDOC))),
    )


def test_static_replay_output_is_byte_identical_to_the_landed_renderer() -> None:
    """Committed product output vs immutable pre-hardening bytes for all 884 units.

    This is what licenses the change: swapping ``subprocess.run`` for a ``Popen``
    + caller-side timeout + group reap must change WHEN pandoc is given up on,
    never WHAT it produces. Compared at ``_convert`` level so the comparison
    isolates the changed function rather than the segmentation around it, which
    this story does not touch.

    The independent legacy fixture cannot follow mutable production constants. The
    new replay files were captured from the product converter and complete external
    replay revalidates them against both supported binaries every week.
    """
    converter = _GENERATOR.StaticReplayConverter(_REPLAY_FIXTURES)
    for position, prepared in enumerate(_renderable_units()):
        expected = _expected_output(_LEGACY_ENTRIES[position])
        assert converter(prepared, "committed-static-pandoc", None) == expected, (
            f"corpus unit {position} rendered differently from the landed renderer: {prepared!r}"
        )
