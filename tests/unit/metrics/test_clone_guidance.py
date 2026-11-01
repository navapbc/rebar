"""Held-out documentation contract for efficient repository cloning (ticket 42fa)."""

from __future__ import annotations

from pathlib import Path


def test_clone_guidance_records_measurements_tradeoff_and_index_link() -> None:
    root = Path(__file__).resolve().parents[3]
    guide = root / "docs" / "clone-guidance.md"
    assert guide.is_file()

    text = guide.read_text(encoding="utf-8").lower()
    for required in (
        "--filter=blob:none",
        "1.04 gib",
        "59 s",
        "68.7 mib",
        "14.3 s",
        "--filter=tree:0",
        "3.57 gib",
        "git blame",
        "degradation",
    ):
        assert required in text
    assert "cannot" in text and "clone" in text and "filter" in text

    index = (root / "docs" / "README.md").read_text(encoding="utf-8")
    assert "[clone-guidance.md](clone-guidance.md)" in index
