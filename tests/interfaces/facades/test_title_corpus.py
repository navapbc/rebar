"""Held-out cross-facade title corpus for story 5977.

``test_title_validation.py`` pins the core whitespace-rejection behavior; this module
is the wider oracle: it verifies that legitimate titles round-trip **byte-identically**
through library / CLI / MCP (no silent normalization), that the ONE documented
exception (U+2192 ``→`` → ``->``) holds, that the >255 guard fires on the create path,
that NUL (argv-impossible) is handled on the library/MCP surfaces, and that
whitespace-only rejection carries the SAME human-readable substring on all three
facade-appropriate surfaces.

Round-trip oracle: a title is byte-identical iff the value read back from ``show`` is
``==`` the value written. NFC and NFD spellings of the same grapheme are DISTINCT byte
strings and are asserted as separate identity cases (never cross-form-equal).
"""

from __future__ import annotations

import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

import rebar

# ── corpus of legitimate titles that must survive verbatim ────────────────────
_NFC = unicodedata.normalize("NFC", "café")  # é as one code point (U+00E9)
_NFD = unicodedata.normalize("NFD", "café")  # e + combining acute (U+0065 U+0301)
BYTE_IDENTITY_CORPUS = [
    "-x leading single dash",
    "--x leading double dash",
    "quotes \"double\" and 'single'",
    "back\\slash and /forward",
    "embedded\nLF newline",
    "embedded\r\nCRLF newline",
    "emoji 😀 and non-BMP 𝔘 chars",
    _NFC,
    _NFD,
    "x" * 255,  # the maximum valid length (the >255 cap is exercised separately)
]


def _cli(*args: str, repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )


def _mcp_create(title: str, repo: Path) -> tuple[bool, str, str | None]:
    """Attempt an MCP create_ticket. Returns (rejected, message, created_id)."""
    import asyncio

    from rebar.mcp_server import build_server

    srv = build_server()
    try:
        res = asyncio.run(srv.call_tool("create_ticket", {"ticket_type": "task", "title": title}))
        # _unwrap-ish: dig the id out of whatever shape FastMCP returns.
        payload = res[1] if isinstance(res, tuple) and len(res) > 1 else res
        if isinstance(payload, dict):
            inner = payload.get("result", payload)
            cid = inner.get("id") if isinstance(inner, dict) else None
        else:
            cid = None
        return False, "", cid
    except Exception as exc:  # noqa: BLE001 — any tool failure is a rejection here
        cause = exc.__cause__ or exc
        return True, f"{exc} {cause}", None


# ── byte-identity round-trip (library) ────────────────────────────────────────
@pytest.mark.parametrize("title", BYTE_IDENTITY_CORPUS)
def test_library_create_round_trips_title_byte_identically(rebar_repo: Path, title: str) -> None:
    tid = rebar.create_ticket("task", title, repo_root=str(rebar_repo))
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["title"] == title


def test_nfc_and_nfd_are_distinct_values_each_preserved(rebar_repo: Path) -> None:
    assert _NFC != _NFD, "corpus precondition: the two forms differ byte-wise"
    a = rebar.create_ticket("task", _NFC, repo_root=str(rebar_repo))
    b = rebar.create_ticket("task", _NFD, repo_root=str(rebar_repo))
    ta = rebar.show_ticket(a, repo_root=str(rebar_repo))["title"]
    tb = rebar.show_ticket(b, repo_root=str(rebar_repo))["title"]
    assert ta == _NFC and tb == _NFD  # each preserved in its own form
    assert ta != tb  # never silently cross-normalized to equality


# ── byte-identity round-trip (MCP), argv-safe subset via CLI ──────────────────
@pytest.mark.parametrize("title", BYTE_IDENTITY_CORPUS)
def test_mcp_create_round_trips_title_byte_identically(rebar_repo: Path, title: str) -> None:
    pytest.importorskip("mcp")
    rejected, msg, cid = _mcp_create(title, rebar_repo)
    assert not rejected, f"MCP wrongly rejected a legitimate title: {msg}"
    assert cid, "MCP create returned no id"
    assert rebar.show_ticket(cid, repo_root=str(rebar_repo))["title"] == title


@pytest.mark.parametrize(
    "title",
    ["quotes \"double\" and 'single'", "emoji 😀 and non-BMP 𝔘 chars", _NFC, _NFD, "x" * 255],
)
def test_cli_create_round_trips_argv_safe_titles(rebar_repo: Path, title: str) -> None:
    # Leading-dash and NUL titles are argv-hostile; those are covered on the
    # library/MCP surfaces. Everything argv-safe must survive the CLI verbatim.
    cp = _cli("create", "task", title, "--output", "json", repo=rebar_repo)
    assert cp.returncode == 0, cp.stderr
    import json

    out = cp.stdout.strip()
    tid = json.loads(out)["id"] if out.startswith("{") else out.splitlines()[-1]
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["title"] == title


# ── the ONE documented normalization exception: U+2192 → "->" ─────────────────
def test_u2192_arrow_is_normalized_to_ascii_arrow(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "flow a→b→c", repo_root=str(rebar_repo))
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["title"] == "flow a->b->c"


# ── >255 create-path rejection (a ≥256-char title cannot round-trip) ──────────
@pytest.mark.parametrize("n", [256, 1000])
def test_create_rejects_over_255_char_title(rebar_repo: Path, n: int) -> None:
    with pytest.raises(rebar.RebarError) as ei:
        rebar.create_ticket("task", "x" * n, repo_root=str(rebar_repo))
    assert "255" in str(ei.value), str(ei.value)


# ── NUL (argv-impossible) round-trips byte-identically on library + MCP ───────
def test_nul_title_round_trips_library_and_mcp(rebar_repo: Path) -> None:
    """A NUL byte is argv-impossible (so untestable via the CLI) but, per the
    preserve-as-is policy, it is NOT special-cased: it round-trips byte-identically
    through the library and MCP surfaces (verified against real store behavior)."""
    title = "bad\x00nul"
    tid = rebar.create_ticket("task", title, repo_root=str(rebar_repo))
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["title"] == title

    pytest.importorskip("mcp")
    rejected, msg, cid = _mcp_create(title, rebar_repo)
    assert not rejected, f"MCP wrongly rejected a NUL-bearing title: {msg}"
    assert cid and rebar.show_ticket(cid, repo_root=str(rebar_repo))["title"] == title


# ── cross-facade rejection identity for whitespace-only titles ────────────────
def test_whitespace_only_rejected_identically_across_all_facades(rebar_repo: Path) -> None:
    pytest.importorskip("mcp")
    bad = "  \t \n "

    # library
    with pytest.raises(rebar.RebarError) as lib_ei:
        rebar.create_ticket("task", bad, repo_root=str(rebar_repo))
    assert "non-empty" in str(lib_ei.value).lower()

    # cli — nonzero exit + message on stderr
    cp = _cli("create", "task", bad, repo=rebar_repo)
    assert cp.returncode != 0
    assert "non-empty" in cp.stderr.lower()

    # mcp — error result carrying the same substring
    rejected, msg, _ = _mcp_create(bad, rebar_repo)
    assert rejected and "non-empty" in msg.lower(), msg
