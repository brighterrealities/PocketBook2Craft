"""Tests for the Craft block helpers in formatter.py."""

from __future__ import annotations

from datetime import datetime, timezone

from pb2craft import formatter

from tests.conftest import make_book, make_highlight


def test_to_craft_blocks_splits_on_separators():
    md = "## Header\nfoo\n\n---\n\n### One\nbar\n\n---\n\n### Two\nbaz"
    blocks = formatter.to_craft_blocks(md)
    assert len(blocks) == 3
    assert all(b["type"] == "text" for b in blocks)
    assert blocks[0]["markdown"].startswith("## Header")
    assert blocks[1]["markdown"].startswith("### One")
    assert blocks[2]["markdown"].startswith("### Two")


def test_to_craft_blocks_handles_empty_chunks():
    md = "\n---\n\n---\nactual\n---\n"
    blocks = formatter.to_craft_blocks(md)
    # Only chunks with non-whitespace content should survive
    assert len(blocks) == 1
    assert blocks[0]["markdown"] == "actual"


def test_to_craft_blocks_end_to_end_with_format_book():
    book = make_book(title="The Book", path="/Jane Doe - The Book.epub")
    highlights = [
        make_highlight(id="h1", text="first", anchor="pbr:/page?page=1"),
        make_highlight(id="h2", text="second", anchor="pbr:/page?page=2"),
    ]
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)

    result = formatter.format_book(book, highlights, now=now)
    blocks = formatter.to_craft_blocks(result.markdown)

    # Expect: 1 metadata block + 2 highlight blocks
    assert len(blocks) == 3
    assert "## Book Highlights" in blocks[0]["markdown"]
    assert "**Author**: Jane Doe" in blocks[0]["markdown"]
    assert "Highlight 1" in blocks[1]["markdown"]
    assert "Highlight 2" in blocks[2]["markdown"]


def test_format_highlights_only_numbers_from_start_index():
    h1 = make_highlight(id="h1", text="one", begin="epubcfi(/6/14!/4/2/1:0)", end="epubcfi(/6/14!/4/2/1:5)")
    h2 = make_highlight(id="h2", text="two", begin="epubcfi(/6/14!/4/2/1:10)", end="epubcfi(/6/14!/4/2/1:15)")

    md = formatter.format_highlights_only([h1, h2], start_index=6)

    assert "Highlight 6" in md
    assert "Highlight 7" in md
    assert "Highlight 1" not in md


def test_format_highlights_only_round_trips_through_blocks():
    h1 = make_highlight(id="h1", text="alpha", begin="epubcfi(/6/14!/4/2/1:0)", end="epubcfi(/6/14!/4/2/1:5)")
    h2 = make_highlight(id="h2", text="beta", begin="epubcfi(/6/14!/4/2/1:10)", end="epubcfi(/6/14!/4/2/1:15)")

    md = formatter.format_highlights_only([h1, h2], start_index=1)
    blocks = formatter.to_craft_blocks(md)

    assert len(blocks) == 2
