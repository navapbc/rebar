"""Tests for the env-var registry generator (story 0f21 / audit maintainability #3)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN_PATH = REPO_ROOT / "scripts" / "gen_env_registry.py"


def _load_gen():
    spec = importlib.util.spec_from_file_location("gen_env_registry", GEN_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load_gen()


def test_positive_capture_direct_helper_and_llm():
    reads, _dynamic = gen.scan(gen.DEFAULT_SCAN_ROOT)
    # a direct os.environ read
    assert "GERRIT_BOT_TOKEN" in reads
    # a _rebar_env("SUFFIX") reconciler read resolved with the REBAR_ prefix
    assert "REBAR_RECONCILER_VERBOSE" in reads
    # a _llm_int(table, cli, "REBAR_LLM_TIMEOUT", ...) read
    assert "REBAR_LLM_TIMEOUT" in reads
    # a _severities_env review-bot read
    assert "BLOCKING_SEVERITIES" in reads


def test_aliases_present_and_removed_vars_absent():
    doc = gen.render()
    # a live permanent alias appears with its annotation
    assert "REBAR_NO_SYNC" in doc
    assert "permanent alias of `REBAR_SYNC_PULL`" in doc
    # vars removed pre-1.0 are NOT emitted (no phantom rows)
    assert "`REBAR_PUSH`" not in doc
    assert "REBAR_MCP_ALLOW_RECONCILE_LIVE" not in doc


def test_drift_is_detected_for_a_new_read(tmp_path: Path):
    # A synthetic module with a NEW env read inside the scanned tree must be picked up,
    # so the drift gate genuinely detects an un-regenerated addition.
    pkg = tmp_path / "rebar_fake"
    pkg.mkdir()
    (pkg / "mod.py").write_text('import os\nX = os.environ["REBAR_FAKE_NEW"]\n')
    reads, _ = gen.scan(tmp_path)
    assert "REBAR_FAKE_NEW" in reads


def test_check_mode_clean_against_committed_tree():
    # The committed docs/env-vars.md must match the generator output (exit 0).
    assert gen.main(["--check"]) == 0


# --- bug 739f: read shapes other than ``.get`` were a SILENT skip -------------------
# The recogniser matched only ``os.environ.get`` / ``os.environ[...]`` / ``os.getenv``.
# Any other ``os.environ`` access fell through ``ast.walk`` with no branch and no
# diagnostic, so the key it read simply never reached ``docs/env-vars.md`` while the
# drift gate stayed green (the doc still agreed with the blind generator). Proven at
# runtime: swapping one ``.get`` to ``.pop`` took the registry 162 -> 161 keys at exit 0.


def _scan_source(tmp_path: Path, source: str):
    """Scan a one-module synthetic tree and return the generator's ``(reads, dynamic)``."""
    pkg = tmp_path / "rebar_fake"
    pkg.mkdir()
    (pkg / "mod.py").write_text("import os\n" + source)
    return gen.scan(tmp_path)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('X = os.environ.pop("REBAR_FAKE_POP", None)\n', "REBAR_FAKE_POP"),
        ('X = os.environ.setdefault("REBAR_FAKE_SETDEFAULT", "v")\n', "REBAR_FAKE_SETDEFAULT"),
        ('X = os.environ.get("REBAR_FAKE_GET")\n', "REBAR_FAKE_GET"),
        ('X = os.environ["REBAR_FAKE_SUB"]\n', "REBAR_FAKE_SUB"),
        ('X = os.getenv("REBAR_FAKE_GETENV")\n', "REBAR_FAKE_GETENV"),
    ],
)
def test_every_key_bearing_read_shape_is_registered(tmp_path: Path, source: str, expected: str):
    # A read is a read regardless of which key-bearing accessor spells it: ``pop`` and
    # ``setdefault`` both return the current value, so both must document their key.
    reads, _dynamic = _scan_source(tmp_path, source)
    assert expected in reads, f"{source.strip()} must register {expected}"


def test_unrecognised_environ_access_fails_loudly(tmp_path: Path):
    # The fail-closed half: a shape the recogniser does NOT understand must abort the
    # generator naming the site, not be walked past. This is what catches the read form
    # nobody predicted -- enumerating accessors alone cannot.
    with pytest.raises(gen.UnrecognisedEnvironAccess) as excinfo:
        _scan_source(tmp_path, 'X = os.environ.somenovelaccessor("REBAR_FAKE_NOVEL")\n')
    message = str(excinfo.value)
    assert "somenovelaccessor" in message, "the error must name the unrecognised accessor"
    assert "mod.py" in message, "the error must name the module"
    assert ":2" in message, "the error must name the line"


@pytest.mark.parametrize(
    "source",
    [
        "X = os.environ.copy()\n",
        "X = list(os.environ.items())\n",
        "X = list(os.environ.keys())\n",
        "X = list(os.environ.values())\n",
    ],
)
def test_bulk_environ_access_is_allowed_and_registers_nothing(tmp_path: Path, source: str):
    # The other half of fail-closed: whole-mapping accesses name no single variable, so
    # they are legitimately unregistrable and must NOT be treated as an unknown shape.
    reads, _dynamic = _scan_source(tmp_path, source)
    assert reads == {}, "a bulk access names no variable, so it registers none"


def test_non_literal_pop_key_is_reported_as_dynamic(tmp_path: Path):
    # A pop whose key is computed is not silently dropped either: it lands in the
    # dynamically-constructed section, exactly as a computed ``.get`` already does.
    _reads, dynamic = _scan_source(tmp_path, "N = 'REBAR_X'\nX = os.environ.pop(N, None)\n")
    assert any(callee == "os.environ.pop" for _mod, _line, callee in dynamic), (
        f"a non-literal pop must be reported as dynamic, got {dynamic}"
    )


def test_committed_registry_still_documents_the_popped_opcert_key():
    # The specific instance that exposed this bug (illusory-patronal-giraffe): the opcert
    # private key is read then popped. It must stay documented, and the registry must not
    # shrink below the 162 keys observed at the time of the fix.
    doc = (REPO_ROOT / "docs" / "env-vars.md").read_text()
    assert "REBAR_OPCERT_PRIVATE_KEY" in doc
    count = int(re.search(r"_(\d+) variables\._", doc).group(1))
    assert count >= 162, f"registry shrank to {count} variables"


def test_dunder_getitem_call_registers_its_key(tmp_path: Path):
    # ``os.environ.__getitem__("X")`` is an explicit CALL, not a Subscript node, so the
    # subscript branch never sees it. It takes the variable name as its first argument and
    # returns that variable's value, which makes it key-bearing: classifying it as a bulk
    # whole-mapping access would reopen the exact silent-skip hole this bug is about.
    reads, _dynamic = _scan_source(tmp_path, 'X = os.environ.__getitem__("REBAR_FAKE_DUNDER")\n')
    assert "REBAR_FAKE_DUNDER" in reads


# --- bug ff2e: KNOWN_ENV_HELPERS was fail-OPEN --------------------------------------
# The os.environ half fails closed (above), but helper recognition did not. A function
# whose os.environ key comes from its own PARAMETER is an env-read helper by construction:
# the literal key lives at its CALL SITES, one level up, and is only resolved when the
# callee name is in KNOWN_ENV_HELPERS. A helper absent from that table was walked past
# silently -- its internal read landed in the "dynamic" list, indistinguishable from a
# registered helper's, so the artifact could not show that keys had been lost. MEASURED:
# `_gate_str_pref` cost REBAR_GATE_REF and REBAR_GATE_SOURCE (scan 120 -> 122 on adding
# the row, --check exit 0 throughout).


def test_unregistered_parameterised_env_helper_aborts_the_scan(tmp_path: Path):
    # THE oracle for this bug. The helper shape is chosen to escape BOTH pre-existing
    # guards: it is not an `_llm_*`/`env_name` resolver in llm/config.py (the b00f guard in
    # test_env_registry_helper_coverage.py), and `lookup_setting` does not match
    # check_config_ownership.py's `_SHIM_RE` (^_.*(env|pref|getenv)). Against today's tree
    # the scan returns silently and REBAR_FAKE_UNREGISTERED is simply absent.
    source = (
        "def lookup_setting(env_name, default=None):\n"
        "    return os.environ.get(env_name, default)\n"
        "\n"
        "VALUE = lookup_setting('REBAR_FAKE_UNREGISTERED')\n"
    )
    with pytest.raises(RuntimeError) as excinfo:
        _scan_source(tmp_path, source)
    message = str(excinfo.value)
    assert "lookup_setting" in message, "the error must name the unregistered helper"
    assert "mod.py" in message, "the error must name the module"
    assert ":2" in message, "the error must name the definition line"


def test_inline_literal_reader_is_not_mistaken_for_a_helper(tmp_path: Path):
    # THE false-positive control, and the reason the trigger is the PARAMETER dataflow
    # rather than "a function body containing an env read" (which would fire on ~97
    # functions in the real tree). This function reads a fixed literal inline, so the
    # scanner already captures its key at the read site -- nothing is lost, nothing to fail.
    reads, _dynamic = _scan_source(
        tmp_path,
        "def read_port():\n    return os.environ.get('REBAR_FAKE_INLINE', '0')\n",
    )
    assert "REBAR_FAKE_INLINE" in reads, "an inline literal read is registered as usual"


def test_registered_helper_does_not_abort_the_scan(tmp_path: Path):
    # The other half of fail-closed: once a helper IS in the table its keys are resolved at
    # the call sites, so the parameterised read inside it is expected, not an offence.
    helper = "_llm_float"
    assert helper in gen.KNOWN_ENV_HELPERS, "re-target this control"
    reads, _dynamic = _scan_source(
        tmp_path,
        f"def {helper}(table, cli, env_name, file_key=None, default=None):\n"
        "    return os.environ.get(env_name, default)\n"
        "\n"
        f"VALUE = {helper}(None, None, 'REBAR_FAKE_REGISTERED')\n",
    )
    assert "REBAR_FAKE_REGISTERED" in reads, "a registered helper resolves its call-site key"


def test_real_tree_scans_clean_and_documents_the_gate_vars():
    # The measured instance. `_gate_str_pref` in src/rebar/_config_resolvers.py reads
    # os.environ.get(env_name); its two literals were invisible. The seam comment directly
    # above it (_config_resolvers.py:288-289) asserts these literals "stay visible to
    # gen_env_registry here" -- this is that claim, enforced.
    reads, _dynamic = gen.scan(gen.DEFAULT_SCAN_ROOT)
    missing = [v for v in ("REBAR_GATE_REF", "REBAR_GATE_SOURCE") if v not in reads]
    assert missing == [], f"read under src/rebar but invisible to the generator: {missing}"
