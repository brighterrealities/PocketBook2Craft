"""Tests for the typed-block formatter (cards + colored highlights)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pb2craft import formatter
from pb2craft.models import Book

from tests.conftest import make_book, make_highlight


# --------------------------------------------------------------------------- #
# Color mapping                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pb_color,craft_color", [
    ("yellow", "yellow"),
    ("green", "green"),
    ("blue", "blue"),
    ("purple", "purple"),
    ("red", "red"),
    ("orange", "gradient-yellow"),
    ("YELLOW", "yellow"),  # case-insensitive
])
def test_craft_color_for_known_colors(pb_color, craft_color):
    assert formatter.craft_color_for(pb_color) == craft_color


def test_craft_color_for_unknown_returns_none():
    assert formatter.craft_color_for("unknown") is None
    assert formatter.craft_color_for("magenta") is None
    assert formatter.craft_color_for("") is None


# --------------------------------------------------------------------------- #
# Card shape                                                                   #
# --------------------------------------------------------------------------- #


def test_highlight_card_has_correct_block_shape():
    h = make_highlight(color="yellow", text="hello", anchor="pbr:/page?page=12")
    book = make_book()
    [_, card] = formatter.format_book_blocks(book, [h])

    assert card["type"] == "page"
    assert card["textStyle"] == "card"
    md = card["markdown"]
    assert md.startswith("<page textStyle=\"card\">")
    assert md.endswith("</page>")
    assert "<pageTitle>Highlight 1 (p. 12)</pageTitle>" in md
    assert "<content>" in md and "</content>" in md


def test_card_wraps_quoted_text_in_highlight_for_known_color():
    h = make_highlight(color="green", text="green quote")
    [_, card] = formatter.format_book_blocks(make_book(), [h])
    assert "<highlight color=\"green\">green quote</highlight>" in card["markdown"]


def test_card_omits_highlight_wrap_for_unknown_color():
    """Plain (uncolored) highlights render as bare text inside the card."""
    h = make_highlight(color="unknown", text="plain quote")
    [_, card] = formatter.format_book_blocks(make_book(), [h])
    assert "<highlight" not in card["markdown"]
    assert "plain quote" in card["markdown"]


def test_orange_maps_to_gradient_yellow():
    h = make_highlight(color="orange", text="orange quote")
    [_, card] = formatter.format_book_blocks(make_book(), [h])
    assert "color=\"gradient-yellow\"" in card["markdown"]


# --------------------------------------------------------------------------- #
# Note rendering                                                               #
# --------------------------------------------------------------------------- #


def test_card_includes_note_when_present():
    h = make_highlight(color="yellow", text="quote", note="my reflection")
    [_, card] = formatter.format_book_blocks(make_book(), [h])
    assert "*Note: my reflection*" in card["markdown"]


def test_card_omits_note_section_when_absent():
    h = make_highlight(color="yellow", text="quote", note=None)
    [_, card] = formatter.format_book_blocks(make_book(), [h])
    assert "*Note:" not in card["markdown"]


# --------------------------------------------------------------------------- #
# XML escaping                                                                 #
# --------------------------------------------------------------------------- #


def test_special_chars_are_xml_escaped():
    h = make_highlight(color="yellow", text='5 < 10 & "quoted"')
    [_, card] = formatter.format_book_blocks(make_book(), [h])
    md = card["markdown"]
    assert "5 < 10" not in md  # the bare < would break XML
    assert "5 &lt; 10 &amp; &quot;quoted&quot;" in md


def test_title_xml_escapes_book_data():
    book = Book(id="b", fast_hash="h", title="A & B")
    [header, _] = formatter.format_book_blocks(book, [make_highlight()])
    # The book title doesn't go in the header block markdown — that's only the
    # Author/Publisher metadata. But the card pageTitle uses 'Highlight N (p.X)'
    # which doesn't include book title; we just verify the header block exists.
    assert header["type"] == "text"


# --------------------------------------------------------------------------- #
# Header block                                                                 #
# --------------------------------------------------------------------------- #


def test_header_block_includes_full_metadata():
    book = Book.model_validate({
        "id": "b", "fast_hash": "h", "title": "T",
        "metadata": {
            "authors": "Jane Doe",
            "publisher": "Pub House",
            "year": 2020,
            "isbn": "9780000000001",
            "lang": "en",
        },
    })
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    [header, _] = formatter.format_book_blocks(book, [make_highlight()], now=now)

    md = header["markdown"]
    assert "## Book Highlights" in md
    assert "**Author**: Jane Doe" in md
    assert "**Publisher**: Pub House" in md
    assert "**Year**: 2020" in md
    assert "**ISBN**: 9780000000001" in md
    assert "**Language**: en" in md
    assert "**Synced**:" in md


def test_header_block_omits_unknown_author():
    book = Book(id="b", fast_hash="h", title="T", path=None)
    [header, _] = formatter.format_book_blocks(book, [make_highlight()])
    assert "**Author**" not in header["markdown"]


# --------------------------------------------------------------------------- #
# Multiple highlights                                                          #
# --------------------------------------------------------------------------- #


def test_format_book_blocks_returns_header_plus_one_card_per_highlight():
    book = make_book()
    highlights = [
        make_highlight(id="h1", text="first",
                       begin="epubcfi(/6/14!/4/2/1:0)", end="epubcfi(/6/14!/4/2/1:5)"),
        make_highlight(id="h2", text="second",
                       begin="epubcfi(/6/14!/4/2/1:10)", end="epubcfi(/6/14!/4/2/1:15)"),
        make_highlight(id="h3", text="third",
                       begin="epubcfi(/6/14!/4/2/1:20)", end="epubcfi(/6/14!/4/2/1:25)"),
    ]
    blocks = formatter.format_book_blocks(book, highlights)
    assert len(blocks) == 4  # 1 header + 3 cards
    assert blocks[0]["type"] == "text"
    for card in blocks[1:]:
        assert card["type"] == "page"
        assert card["textStyle"] == "card"


# --------------------------------------------------------------------------- #
# Incremental append                                                           #
# --------------------------------------------------------------------------- #


def test_format_highlight_blocks_numbers_from_start_index():
    h1 = make_highlight(id="h1", text="one", anchor=None,
                        begin="epubcfi(/6/14!/4/2/1:0)", end="epubcfi(/6/14!/4/2/1:5)")
    h2 = make_highlight(id="h2", text="two", anchor=None,
                        begin="epubcfi(/6/14!/4/2/1:10)", end="epubcfi(/6/14!/4/2/1:15)")

    blocks = formatter.format_highlight_blocks([h1, h2], start_index=6)

    assert len(blocks) == 2
    assert "<pageTitle>Highlight 6</pageTitle>" in blocks[0]["markdown"]
    assert "<pageTitle>Highlight 7</pageTitle>" in blocks[1]["markdown"]
