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

**Cost discipline.** Proving that third property over the whole committed corpus
costs ~1768 pandoc spawns — 884 renderable units, each rendered twice (the new
path and the oracle). As ONE test function that measured 91.6s idle but 504.8s
under CPU saturation — past CI's ``--timeout=300``. Because CI passes
``--timeout-method=thread``, pytest-timeout ``os._exit(1)``s the whole xdist
worker rather than failing the test, so the run reported
``worker 'gwN' crashed`` with no traceback (bug 0078-fa3e-05d1-4e12). The
comparison is therefore split into fixed-size chunks — see
:data:`_EQUIVALENCE_CHUNK`. Every unit is still compared; only the per-test
wall clock changes.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

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
_STRATA = ("code_arrow", "table", "prose")

_PANDOC = _pandoc_path()
_NEEDS_PANDOC = pytest.mark.skipif(_PANDOC is None, reason="the `wiki` extra is not installed")


def _bodies() -> list[str]:
    out: list[str] = []
    for stratum in _STRATA:
        out.extend(json.loads((_CORPUS / f"{stratum}.json").read_text(encoding="utf-8")))
    return out


def _legacy_convert(markdown: str, pandoc: str) -> str | None:
    """The renderer's PRE-hardening body, reproduced verbatim as the oracle.

    Kept here rather than imported because the point is to compare against what
    the landed code did, which no longer exists in the module. If this drifts from
    the real conversion contract the equivalence test below stops meaning
    anything, so it is deliberately a literal transcription: same argv, same
    stdin, same post-processing.
    """
    try:
        completed = subprocess.run(
            [
                pandoc,
                "-f",
                wiki_render._PANDOC_FROM,
                "-t",
                wiki_render._PANDOC_TO,
                *wiki_render._PANDOC_ARGS,
            ],
            input=markdown,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return wiki_render._ANCHOR_RE.sub("", completed.stdout).rstrip("\n")


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

    def _spy(argv, **kwargs):  # type: ignore[no-untyped-def]
        seen["argv"] = list(argv)
        seen["stdin"] = kwargs.get("stdin")
        return real_popen(argv, **kwargs)

    import unittest.mock

    with unittest.mock.patch.object(subprocess, "Popen", _spy):
        assert _convert("hello **world**", str(_PANDOC)) is not None

    assert seen["stdin"] == subprocess.PIPE
    assert seen["stdin"] != subprocess.DEVNULL
    argv = seen["argv"]
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


# Every Nth renderable unit. 1 = the WHOLE committed corpus, which is what this
# ships with: full coverage, so the AC is met literally rather than by sample.
# The strata are walked in order (code_arrow, table, prose) so any stride still
# spreads across all three.
_EQUIVALENCE_STRIDE = 1

# Units compared per test case. This bounds the per-test wall clock, which is the
# whole point: at stride 1 the corpus is ~884 units and a single test function
# comparing all of them measured 91.6s idle and 504.8s under CPU saturation — past
# CI's 300s per-test guard, which kills the xdist worker outright (see the module
# docstring). Because the chunk size is FIXED, growing the corpus adds CASES and
# never lengthens one, so the guard cannot be re-crossed by fixture growth. Tune
# this only to trade case count against per-case cost; it changes nothing about
# what is compared.
_EQUIVALENCE_CHUNK = 40


def _equivalence_chunks() -> list[list[str]]:
    """The sampled corpus, sliced into fixed-size batches of units."""
    sampled = _renderable_units()[::_EQUIVALENCE_STRIDE]
    return [sampled[i : i + _EQUIVALENCE_CHUNK] for i in range(0, len(sampled), _EQUIVALENCE_CHUNK)]


_CHUNKS = _equivalence_chunks()


def test_the_equivalence_chunks_cover_every_renderable_unit() -> None:
    """Chunking must not quietly drop units — that would hollow out the check below.

    Cheap and pandoc-free, so it is the one place the corpus-wide invariants live:
    the chunks reassemble to exactly the sampled corpus, in order, and there are
    enough units for the comparison to be evidence rather than a spot check. That
    floor cannot sit inside a chunk, since no single chunk meets it.
    """
    units = _renderable_units()
    assert units, "the corpus produced no renderable units — fixture regression"
    sampled = units[::_EQUIVALENCE_STRIDE]
    assert [unit for chunk in _CHUNKS for unit in chunk] == sampled
    assert len(sampled) >= 100, f"stride too coarse to be evidence: {len(sampled)} units"
    assert all(len(chunk) <= _EQUIVALENCE_CHUNK for chunk in _CHUNKS)


@_NEEDS_PANDOC
@pytest.mark.parametrize("chunk_index", range(len(_CHUNKS)))
def test_rendered_output_is_byte_identical_to_the_landed_renderer(chunk_index: int) -> None:
    """New path vs the pre-hardening oracle, over every unit of the committed corpus.

    This is what licenses the change: swapping ``subprocess.run`` for a ``Popen``
    + caller-side timeout + group reap must change WHEN pandoc is given up on,
    never WHAT it produces. Compared at ``_convert`` level so the comparison
    isolates the changed function rather than the segmentation around it, which
    this story does not touch.

    ``test_wiki_render_corpus.py`` drives the same bodies through ``_convert``
    for its idempotence and no-decay assertions, so a rendering change would
    surface there too — but only as "some property moved". This says the stronger
    thing: the bytes are the same ones the landed renderer produced.

    One chunk per case: the corpus is covered by the parametrization as a whole
    (pinned by the coverage test above), while each case stays a small, fixed
    fraction of the per-test timeout no matter how contended the runner is.
    """
    pandoc = str(_PANDOC)
    offset = chunk_index * _EQUIVALENCE_CHUNK
    for position, prepared in enumerate(_CHUNKS[chunk_index]):
        assert _convert(prepared, pandoc) == _legacy_convert(prepared, pandoc), (
            f"corpus unit {offset + position} rendered differently from the landed "
            f"renderer: {prepared!r}"
        )
