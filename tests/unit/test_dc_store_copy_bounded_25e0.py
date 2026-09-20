"""Repo-tier oracles for the DC store copy's disk cost (``plaid-glass-manxcat``).

WHY THIS LIVES IN THE UNIT TIER. The defect it pins was found in a Live External Integration
run: the rehearsal fixtures copied the whole 1.5 GB ticket store per module, and the job died
with ``fatal: sha1 file '.../index.lock' write error. Out of diskspace`` after the runner
reported ``Free space left: 23 MB``. The cost tracked total store size, which grows
monotonically, so the failure recurs and worsens on its own.

Reproducing that on the live lane needs a booted amd64 Jira DC image and ~40 minutes of live
spend, and it only reproduces once the store is large enough -- which is to say, too late. The
helpers carrying the repair are plain module-level functions, so they are driven here against
real temporary Git repositories on every commit, with no Jira and no CI provider involved.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_HARNESS = Path(__file__).resolve().parents[1] / "external" / "live_jira_dc"
_FIXTURES = _HARNESS / "_dc_fixtures.py"


def _load_fixtures() -> ModuleType:
    """Path-load ``_dc_fixtures`` the way the harness itself does.

    Its sibling ``_dc_support`` is deliberately NOT imported: that module probes live Jira at
    import time, which the repo tier forbids. The slice helpers stand alone for that reason.
    """
    spec = importlib.util.spec_from_file_location("_dc_fixtures_bounded", _FIXTURES)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ticket_names(count: int) -> list[str]:
    """Canonical four-quad ids, distinct and sorted-stable across both store sizes."""
    return [f"{i:04x}-0000-0000-4000" for i in range(count)]


def _build_store(root: Path, tickets: list[str], payload: bytes) -> None:
    """A minimal real tickets branch: one CREATE event per ticket, plus store metadata."""
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "tickets"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "bounded copy test"], cwd=root, check=True)
    (root / ".store-compat.json").write_text("{}\n")
    for name in tickets:
        event = root / name
        event.mkdir()
        (event / "0001-CREATE.json").write_bytes(payload)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "--no-verify", "-m", "store"], cwd=root, check=True)


def _extract(module: ModuleType, source: Path, dest: Path) -> tuple[int, list[str]]:
    """Run the fixture's own extraction against ``source``; return bytes written and entries."""
    dest.mkdir(parents=True)
    # `extract_store_snapshot` archives FETCH_HEAD, which is what the fixture fetches; point it
    # at the local branch the same way a fetch would.
    subprocess.run(["git", "fetch", "-q", str(source), "tickets"], cwd=source, check=True)
    listing = (
        subprocess.run(
            ["git", "ls-tree", "--name-only", "FETCH_HEAD"],
            cwd=source,
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .split()
    )
    entries = module.store_copy_entries(listing)
    module.extract_store_snapshot(source, dest, entries)
    written = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
    return written, entries


def test_copy_size_does_not_scale_with_total_store_size(tmp_path: Path) -> None:
    """Two stores of very different sizes must yield copies of the same bounded size.

    This is the oracle for the runner-disk exhaustion: before the fix the copy was the whole
    branch, so this comparison would show the large store costing ~20x the small one.
    """
    module = _load_fixtures()
    payload = b'{"event":"CREATE","pad":"' + b"x" * 4096 + b'"}\n'
    limit = module.STORE_COPY_TICKET_LIMIT

    small_src = tmp_path / "small-src"
    large_src = tmp_path / "large-src"
    _build_store(small_src, _ticket_names(limit + 10), payload)
    _build_store(large_src, _ticket_names(limit * 20), payload)

    small_bytes, small_entries = _extract(module, small_src, tmp_path / "small-dst")
    large_bytes, large_entries = _extract(module, large_src, tmp_path / "large-dst")

    assert len(small_entries) == len(large_entries), (
        "the slice is not bounded: a 20x larger store selected "
        f"{len(large_entries)} entries against {len(small_entries)}"
    )
    assert large_bytes == small_bytes, (
        f"copy cost still tracks store size: {large_bytes} bytes from the large store vs "
        f"{small_bytes} from the small one"
    )


def test_the_bounded_slice_still_carries_real_tickets_and_store_metadata(tmp_path: Path) -> None:
    """A bounded copy is still a REAL store: real ticket events, and the metadata to converge.

    Thinning to synthetic tickets would relocate the coverage gap rather than close it, so the
    slice is checked for genuine extracted event files, not merely for entry names.
    """
    module = _load_fixtures()
    source = tmp_path / "src"
    _build_store(source, _ticket_names(module.STORE_COPY_TICKET_LIMIT * 5), b'{"e":"CREATE"}\n')

    dest = tmp_path / "dst"
    _, entries = _extract(module, source, dest)

    assert ".store-compat.json" in entries, "store metadata must travel or the copy cannot converge"
    assert (dest / ".store-compat.json").is_file()
    tickets = [p for p in dest.iterdir() if not p.name.startswith(".")]
    assert len(tickets) == module.STORE_COPY_TICKET_LIMIT
    assert all((p / "0001-CREATE.json").read_bytes() == b'{"e":"CREATE"}\n' for p in tickets)


def test_the_slice_spans_the_whole_store_rather_than_one_contiguous_era() -> None:
    """Sampling at a stride is what keeps the copy representative of the store's history.

    A head or tail slice would silently narrow the rehearsal to the oldest or newest tickets.
    """
    module = _load_fixtures()
    names = _ticket_names(1000)

    selected = [e for e in module.store_copy_entries(names, limit=10) if not e.startswith(".")]

    assert len(selected) == 10
    assert selected[0] == names[0]
    assert selected[-1] > names[800], f"the slice stops early at {selected[-1]}"


def test_bridge_named_directories_are_never_thinned_out() -> None:
    """Bridge-named directories are what a Jira rehearsal exercises, so all of them travel."""
    module = _load_fixtures()
    bridged = [f"jira-reb-{i}" for i in range(30)]

    selected = module.store_copy_entries([*_ticket_names(1000), *bridged], limit=5)

    assert set(bridged) <= set(selected)


def test_the_snapshot_is_not_buffered_whole_in_memory() -> None:
    """The archive must be piped into tar, not captured and handed over as bytes.

    Capturing held the whole snapshot in RAM on top of the copy on disk. Read structurally so
    the guarantee cannot regress by someone reinstating a capture elsewhere in the module.
    """
    tree = ast.parse(_FIXTURES.read_text())

    archives = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == "git archive"
    ]
    assert not archives, "git archive is invoked as a list of argv parts, not a shell string"
    buffered = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and any(kw.arg == "input" for kw in node.keywords)
        and any(
            isinstance(arg, ast.List)
            and any(isinstance(e, ast.Constant) and e.value == "tar" for e in arg.elts)
            for arg in node.args
        )
    ]
    assert not buffered, "tar is fed a buffered archive; stream it from git archive's stdout"
    assert "subprocess.Popen" in _FIXTURES.read_text(), "the streaming pipe is gone"


def test_a_failed_archive_is_reported_with_gits_own_message(tmp_path: Path) -> None:
    """A broken extraction must name why, not surface as an empty copy downstream."""
    module = _load_fixtures()
    source = tmp_path / "src"
    _build_store(source, _ticket_names(3), b"{}\n")
    dest = tmp_path / "dst"
    dest.mkdir()

    with pytest.raises(RuntimeError, match="git archive"):
        module.extract_store_snapshot(source, dest, ["no-such-ref-entry-xyz"])


def test_gits_message_wins_even_though_tar_also_fails(tmp_path: Path) -> None:
    """git's failure must be reported even where tar rejects the stream too.

    A failed ``git archive`` writes nothing, and the two tars disagree about an empty input:
    BSD tar (macOS) exits 0, GNU tar (Linux) exits 2 with "This does not look like a tar
    archive". Checking tar first therefore raised ``CalledProcessError`` on Linux while
    passing on macOS -- green locally, red on all 3 CI Python versions.

    Point tar at a directory that does not exist so it fails on EVERY platform rather than
    only where the empty stream offends it. Then both commands have failed, and the assertion
    is purely about which one is reported: git's, because it says why.
    """
    module = _load_fixtures()
    source = tmp_path / "src"
    _build_store(source, _ticket_names(3), b"{}\n")

    with pytest.raises(RuntimeError) as caught:
        module.extract_store_snapshot(source, tmp_path / "absent", ["no-such-ref-entry-xyz"])

    assert "git archive" in str(caught.value)
    assert "tar extraction" not in str(caught.value)


def test_a_failed_extraction_is_reported_with_tars_own_message(tmp_path: Path) -> None:
    """When git succeeds and only tar fails, the caller still gets a named cause."""
    module = _load_fixtures()
    source = tmp_path / "src"
    names = _ticket_names(3)
    _build_store(source, names, b"{}\n")
    # extract_store_snapshot archives FETCH_HEAD, so establish one the way a real fetch would;
    # without it git fails first and this test would assert the OTHER branch by accident.
    subprocess.run(["git", "fetch", "-q", str(source), "tickets"], cwd=source, check=True)

    with pytest.raises(RuntimeError, match="tar extraction"):
        module.extract_store_snapshot(source, tmp_path / "absent", [names[0]])


def test_reclaim_removes_a_finished_copy(tmp_path: Path) -> None:
    """Teardown must reclaim the copy so concurrent modules do not sum their peaks."""
    module = _load_fixtures()
    work = tmp_path / "dc-store-copy"
    (work / ".tickets-tracker" / "0001-0000-0000-4000").mkdir(parents=True)
    (work / ".tickets-tracker" / "0001-0000-0000-4000" / "e.json").write_text("{}")

    module.reclaim(work)

    assert not work.exists()
    module.reclaim(work)  # idempotent: a second teardown must not raise


@pytest.mark.parametrize("fixture_file", ["_dc_fixtures.py", "test_multi_project_rehearsal.py"])
def test_every_store_copy_fixture_reclaims_its_tree(fixture_file: str) -> None:
    """Both copy sites must reclaim, or one module's leftovers still fill the disk."""
    source = (_HARNESS / fixture_file).read_text()

    assert "reclaim(work)" in source, f"{fixture_file} never reclaims its store copy"
    assert "yield work" in source, f"{fixture_file} cannot reclaim: it returns instead of yielding"


def test_the_local_ticket_entry_rule_matches_the_harnesss_own() -> None:
    """The duplicated predicate must stay identical to `_dc_support.is_ticket_entry`.

    `_dc_fixtures` cannot import that sibling -- it probes live Jira at import -- so the two
    are compared structurally here instead, which is what stops them drifting apart.
    """
    shared = _body_of(_HARNESS / "_dc_support.py", "is_ticket_entry")
    local = _body_of(_FIXTURES, "_is_ticket_entry")

    assert shared == local, f"ticket-entry rules diverged: {shared!r} vs {local!r}"


def _body_of(path: Path, name: str) -> str:
    """The unparsed return expression of a one-line predicate, ignoring its docstring."""
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            returns = [n for n in node.body if isinstance(n, ast.Return)]
            assert len(returns) == 1, f"{name} is no longer a single-expression predicate"
            assert returns[0].value is not None
            return ast.unparse(returns[0].value)
    raise AssertionError(f"{name} not found in {path}")
