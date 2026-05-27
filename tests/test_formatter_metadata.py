"""Tests for the expanded book metadata block in formatter output."""

from __future__ import annotations

from pb2craft import formatter
from pb2craft.models import Book

from tests.conftest import make_highlight


def _book_with_metadata(**meta_overrides):
    meta = {
        "authors": "Jane Doe",
        "publisher": "Pub House",
        "isbn": "9780000000001",
        "lang": "en",
        "year": "2020",
    }
    meta.update(meta_overrides)
    return Book.model_validate({
        "id": "b1",
        "fast_hash": "h1",
        "title": "Book Title",
        "metadata": meta,
    })


def test_format_book_renders_full_metadata():
    book = _book_with_metadata()
    md = formatter.format_book(book, [make_highlight()])
    assert "**Author**: Jane Doe" in md.markdown
    assert "**Publisher**: Pub House" in md.markdown
    assert "**Year**: 2020" in md.markdown
    assert "**ISBN**: 9780000000001" in md.markdown
    assert "**Language**: en" in md.markdown


def test_format_book_omits_null_metadata_fields():
    book = _book_with_metadata(publisher=None, year=None, isbn=None, lang=None)
    md = formatter.format_book(book, [make_highlight()])
    assert "**Author**: Jane Doe" in md.markdown
    assert "**Publisher**" not in md.markdown
    assert "**Year**" not in md.markdown
    assert "**ISBN**" not in md.markdown
    assert "**Language**" not in md.markdown


def test_format_book_no_metadata_block_when_no_metadata():
    book = Book(id="b", fast_hash="h", title="Bare")
    md = formatter.format_book(book, [make_highlight()])
    assert "**Publisher**" not in md.markdown
    assert "**ISBN**" not in md.markdown
