"""Pytest fixtures and builders for unit tests."""

from __future__ import annotations

from datetime import datetime, timezone

from pb2craft.models import Book, Highlight, HighlightColor, Mark, Quotation


def make_highlight(
    *,
    id: str = "h1",
    uuid: str | None = None,
    book_id: str = "book-1",
    book_fast_hash: str = "fasthash-1",
    color: str = "yellow",
    text: str = "Some highlighted text",
    note: str | None = None,
    begin: str = "epubcfi(/6/14!/4/2/1:0)",
    end: str = "epubcfi(/6/14!/4/2/1:10)",
    updated: datetime | None = None,
    anchor: str | None = "pbr:/page?page=1&offs=0",
    created: datetime | None = None,
) -> Highlight:
    return Highlight(
        id=id,
        uuid=uuid or id,
        book_id=book_id,
        book_fast_hash=book_fast_hash,
        color=HighlightColor(value=color),
        text=text,
        note=note,
        quotation=Quotation(begin=begin, end=end, text=text, updated=updated),
        mark=Mark(anchor=anchor, created=created),
    )


def make_book(
    *,
    id: str = "book-1",
    fast_hash: str = "fasthash-1",
    title: str = "Sample Book",
    path: str | None = "/Author Name - Sample Book.epub",
    collections: str | None = None,
    link: str | None = None,
) -> Book:
    return Book(
        id=id,
        fast_hash=fast_hash,
        title=title,
        path=path,
        collections=collections,
        link=link,
    )


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
