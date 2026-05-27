"""Tests for the new Book metadata + cover fields."""

from __future__ import annotations

from pb2craft.models import Book, BookCover, BookMetadata


SAMPLE_PAYLOAD = {
    "id": "67721922",
    "fast_hash": "dd29d5",
    "title": "THE MASTER AND HIS EMISSARY",
    "path": "/Some Author - Title.epub",
    "metadata": {
        "authors": "Iain McGilchrist",
        "publisher": "Yale University Press",
        "isbn": "9780300170177",
        "lang": "en",
        "year": None,
        "cover": [
            {"width": 260, "height": 400, "path": "https://example/cover_s.jpg"},
            {"width": 520, "height": 800, "path": "https://example/cover_b.jpg"},
        ],
    },
}


def test_book_parses_metadata():
    book = Book.model_validate(SAMPLE_PAYLOAD)
    assert book.metadata is not None
    assert book.metadata.authors == "Iain McGilchrist"
    assert book.metadata.publisher == "Yale University Press"
    assert book.metadata.isbn == "9780300170177"
    assert len(book.metadata.cover) == 2


def test_display_authors_prefers_metadata():
    book = Book.model_validate(SAMPLE_PAYLOAD)
    # Path has "Some Author - Title.epub" but metadata says Iain McGilchrist
    assert book.display_authors == "Iain McGilchrist"


def test_display_authors_falls_back_to_path():
    payload = {
        "id": "b2",
        "fast_hash": "x",
        "title": "T",
        "path": "/Jane Doe - The Book.epub",
        "metadata": {"authors": None},
    }
    book = Book.model_validate(payload)
    assert book.display_authors == "Jane Doe"


def test_largest_cover_url_picks_biggest():
    meta = BookMetadata(
        cover=[
            BookCover(width=260, height=400, path="small"),
            BookCover(width=520, height=800, path="big"),
        ]
    )
    assert meta.largest_cover_url() == "big"


def test_cover_url_none_when_no_metadata():
    book = Book(id="b", fast_hash="h", title="T")
    assert book.cover_url is None


def test_cover_url_none_when_metadata_has_no_cover():
    book = Book.model_validate({"id": "b", "fast_hash": "h", "title": "T", "metadata": {"authors": "X"}})
    assert book.cover_url is None


def test_cover_url_returns_largest():
    book = Book.model_validate(SAMPLE_PAYLOAD)
    assert book.cover_url == "https://example/cover_b.jpg"
