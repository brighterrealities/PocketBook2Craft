"""Regression tests for real-world schema shapes the live API exhibits."""

from __future__ import annotations

from pb2craft.models import Book


def test_year_as_int_is_accepted():
    book = Book.model_validate({
        "id": "b1", "fast_hash": "h", "title": "T",
        "metadata": {"authors": "A", "year": 2015},
    })
    # We accept either; display uses f-string so the underlying type doesn't matter.
    assert book.metadata is not None
    assert book.metadata.year == 2015


def test_null_cover_becomes_empty_list():
    book = Book.model_validate({
        "id": "b1", "fast_hash": "h", "title": "T",
        "metadata": {"authors": "A", "cover": None},
    })
    assert book.metadata is not None
    assert book.metadata.cover == []
    assert book.cover_url is None


def test_isbn_as_int_is_accepted():
    book = Book.model_validate({
        "id": "b1", "fast_hash": "h", "title": "T",
        "metadata": {"isbn": 9780000000000},
    })
    assert book.metadata is not None
    assert book.metadata.isbn == 9780000000000
