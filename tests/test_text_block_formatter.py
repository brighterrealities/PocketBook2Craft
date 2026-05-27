"""Tests for the decoration-aware text-block formatter (the sync hot path)."""

from __future__ import annotations

from datetime import datetime, timezone

from pb2craft import formatter
from pb2craft.models import Book

from tests.conftest import make_book, make_highlight


def test_format_book_text_blocks_returns_header_plus_2_per_highlight():
    book = make_book()
    highlights = [
        make_highlight(id="h1", text="first",
                       begin="epubcfi(/6/14!/4/2/1:0)", end="epubcfi(/6/14!/4/2/1:5)"),
        make_highlight(id="h2", text="second",
                       begin="epubcfi(/6/14!/4/2/1:10)", end="epubcfi(/6/14!/4/2/1:15)"),
    ]
    blocks = formatter.format_book_text_blocks(book, highlights)
    # 1 header + (heading + quote) × 2 highlights = 5 blocks
    assert len(blocks) == 5


def test_decorations_appear_on_quote_block_only():
    book = make_book()
    h = make_highlight(text="hello", anchor=None)
    blocks = formatter.format_book_text_blocks(book, [h], decorations=["quote", "callout"])

    header, heading, quote = blocks
    assert "decorations" not in header
    assert "decorations" not in heading
    assert quote["decorations"] == ["quote", "callout"]


def test_heading_block_uses_h3_style():
    h = make_highlight(text="x", anchor=None)
    _, heading, _ = formatter.format_book_text_blocks(make_book(), [h])
    assert heading["type"] == "text"
    assert heading["textStyle"] == "h3"
    assert heading["markdown"] == "### Highlight 1"


def test_heading_includes_page_number_when_present():
    h = make_highlight(text="x", anchor="pbr:/page?page=42")
    _, heading, _ = formatter.format_book_text_blocks(make_book(), [h])
    assert heading["markdown"] == "### Highlight 1 (p. 42)"


def test_quote_block_has_no_blockquote_prefix():
    """The '>' markdown is gone — Craft renders quote via decorations now."""
    h = make_highlight(color="yellow", text="hello", anchor=None)
    _, _, quote = formatter.format_book_text_blocks(make_book(), [h])
    assert not quote["markdown"].startswith(">")
    assert "<highlight color=\"yellow\">hello</highlight>" in quote["markdown"]


def test_no_decorations_field_when_none():
    """Empty/None decorations → field omitted entirely (Craft falls back to defaults)."""
    h = make_highlight(text="x", anchor=None)

    blocks_none = formatter.format_book_text_blocks(make_book(), [h], decorations=None)
    assert "decorations" not in blocks_none[2]

    blocks_empty = formatter.format_book_text_blocks(make_book(), [h], decorations=[])
    assert "decorations" not in blocks_empty[2]


def test_decorations_list_is_copied_not_referenced():
    """Mutating the caller's list mustn't change the block."""
    h = make_highlight(text="x", anchor=None)
    decos = ["quote"]
    _, _, quote = formatter.format_book_text_blocks(make_book(), [h], decorations=decos)
    decos.append("callout")
    assert quote["decorations"] == ["quote"]


def test_note_is_appended_to_quote_block():
    h = make_highlight(color="yellow", text="quote", note="my thought", anchor=None)
    _, _, quote = formatter.format_book_text_blocks(make_book(), [h])
    assert "*Note: my thought*" in quote["markdown"]


def test_header_block_includes_metadata():
    book = Book.model_validate({
        "id": "b1", "fast_hash": "h", "title": "T",
        "metadata": {"authors": "Jane Doe", "publisher": "Pub", "year": 2020, "isbn": "9780000000001", "lang": "en"},
    })
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    [header, *_] = formatter.format_book_text_blocks(book, [make_highlight()], now=now)
    md = header["markdown"]
    assert "## Book Highlights" in md
    assert "**Author**: Jane Doe" in md
    assert "**Publisher**: Pub" in md
    assert "**Synced**:" in md


def test_format_highlight_text_blocks_for_append():
    h1 = make_highlight(id="h1", text="one", anchor=None,
                        begin="epubcfi(/6/14!/4/2/1:0)", end="epubcfi(/6/14!/4/2/1:5)")
    h2 = make_highlight(id="h2", text="two", anchor=None,
                        begin="epubcfi(/6/14!/4/2/1:10)", end="epubcfi(/6/14!/4/2/1:15)")

    blocks = formatter.format_highlight_text_blocks(
        [h1, h2], decorations=["callout"], start_index=6,
    )
    # 2 highlights × 2 blocks each = 4
    assert len(blocks) == 4
    assert blocks[0]["markdown"] == "### Highlight 6"
    assert blocks[2]["markdown"] == "### Highlight 7"
    assert blocks[1]["decorations"] == ["callout"]
    assert blocks[3]["decorations"] == ["callout"]


def test_xml_escaping_still_applies():
    h = make_highlight(color="yellow", text='5 < 10 & "x"', anchor=None)
    _, _, quote = formatter.format_book_text_blocks(make_book(), [h])
    assert "5 &lt; 10 &amp; &quot;x&quot;" in quote["markdown"]
    assert "5 < 10" not in quote["markdown"]
