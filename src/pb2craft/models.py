"""Domain models — ported from Swift PocketBook2CapacitiesCore/Models.

JSON shape from the PocketBook Cloud API uses snake_case; Pydantic aliases let
us keep idiomatic Python attribute names while parsing the wire format directly.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pb2craft.processing import cfi as _cfi

# --------------------------------------------------------------------------- #
# Highlight                                                                    #
# --------------------------------------------------------------------------- #


class HighlightColor(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    value: str = "unknown"


class Quotation(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    begin: str = ""
    end: str = ""
    text: str = ""
    updated: datetime | None = None


_PAGE_RE = re.compile(r"page=(\d+)")


class Mark(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    anchor: str | None = None
    created: datetime | None = None
    updated: datetime | None = None

    @property
    def page(self) -> int | None:
        """Extract page number from anchor URL like 'pbr:/page?page=36&offs=…'."""
        if not self.anchor:
            return None
        m = _PAGE_RE.search(self.anchor)
        return int(m.group(1)) if m else None


_SENTENCE_TERMINATORS = set('.!?"\'')


class Highlight(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    uuid: str
    book_id: str = Field(alias="bookId")
    book_fast_hash: str = Field(alias="bookFastHash")
    color: HighlightColor
    text: str
    quotation: Quotation
    note: str | None = None
    mark: Mark | None = None

    # ----- derived properties (port of Swift extensions) -------------------- #

    @property
    def has_note(self) -> bool:
        return bool(self.note and self.note.strip())

    @property
    def ends_with_sentence_terminator(self) -> bool:
        trimmed = self.text.strip()
        return bool(trimmed) and trimmed[-1] in _SENTENCE_TERMINATORS

    @property
    def starts_with_lowercase(self) -> bool:
        trimmed = self.text.strip()
        return bool(trimmed) and trimmed[0].islower()

    @property
    def created_timestamp(self) -> datetime | None:
        return self.mark.created if self.mark else None

    @property
    def begin_position(self) -> "_cfi.CFIPosition | None":
        return _cfi.parse(self.quotation.begin)

    @property
    def end_position(self) -> "_cfi.CFIPosition | None":
        return _cfi.parse(self.quotation.end)

    def is_adjacent_to(self, other: "Highlight", threshold: float = 50.0) -> bool:
        my_end = self.end_position
        other_begin = other.begin_position
        if my_end is None or other_begin is None:
            return False
        return _cfi.are_adjacent(my_end, other_begin, threshold=threshold)


# --------------------------------------------------------------------------- #
# Book                                                                         #
# --------------------------------------------------------------------------- #


_AUTHOR_SEPARATORS = [" - ", " _ ", " – ", " — "]  # hyphen, underscore, en-dash, em-dash
_COMMON_TITLE_WORDS = {"the", "a", "an", "of", "and", "in", "to", "for", "with", "on"}


class BookCover(BaseModel):
    """A single rendition of a book's cover image."""

    model_config = ConfigDict(populate_by_name=True)

    width: int = 0
    height: int = 0
    path: str  # URL — for PocketBook this carries an access_token query param


class BookMetadata(BaseModel):
    """Extended metadata from the /books endpoint's `metadata` field.

    Real-world data is loose: ``year`` arrives as int *or* string, ``cover`` is
    either a list or explicit ``null``. We accept both shapes to avoid bombing
    on otherwise-fine books.
    """

    model_config = ConfigDict(populate_by_name=True)

    title: str | None = None
    authors: str | None = None
    publisher: str | None = None
    isbn: int | str | None = None
    lang: str | None = None
    year: int | str | None = None
    series: str | None = None
    annotation: str | None = None
    cover: list[BookCover] = Field(default_factory=list)

    @field_validator("cover", mode="before")
    @classmethod
    def _none_cover_to_empty(cls, v):
        return [] if v is None else v

    def largest_cover_url(self) -> str | None:
        """Return the URL of the largest available cover, or None."""
        if not self.cover:
            return None
        biggest = max(self.cover, key=lambda c: c.width * c.height)
        return biggest.path or None


class Book(BaseModel):
    """A book record from the PocketBook Cloud /books endpoint.

    Wire format is snake_case. We accept both alias and python-name on input
    for ergonomics in tests, but field aliases let us decode raw API JSON.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str
    fast_hash: str = Field(alias="fast_hash")
    title: str = ""
    path: str | None = None
    mime_type: str | None = Field(default=None, alias="mime_type")
    created_at: str | None = Field(default=None, alias="created_at")
    purchased: bool | None = None
    resource_id: str | None = Field(default=None, alias="resource_id")
    bytes_: int | None = Field(default=None, alias="bytes")
    client_mtime: str | None = Field(default=None, alias="client_mtime")
    # Server-side last-modified timestamp. Used to skip per-book highlight
    # fetches when the cloud says nothing has changed since our last sync.
    mtime: str | None = None
    collections: str | None = None
    favorite: bool | None = None
    read_status: str | None = Field(default=None, alias="read_status")
    link: str | None = None
    metadata: BookMetadata | None = None

    @property
    def uuid(self) -> str:
        return self.id

    @property
    def display_title(self) -> str:
        return self.title if self.title else "Untitled"

    @property
    def is_epub(self) -> bool:
        return self.mime_type == "application/epub+zip"

    @property
    def display_authors(self) -> str:
        # Prefer the authoritative metadata field; fall back to path heuristics.
        if self.metadata and self.metadata.authors:
            return self.metadata.authors
        return self._extract_author_from_path() or "Unknown Author"

    @property
    def cover_url(self) -> str | None:
        return self.metadata.largest_cover_url() if self.metadata else None

    def _extract_author_from_path(self) -> str | None:
        if not self.path:
            return None

        filename = self.path.rsplit("/", 1)[-1]
        name = filename.rsplit(".", 1)[0] if "." in filename else filename

        # Pattern 1: "Title by Author"
        m = re.search(r"\s+by\s+", name, flags=re.IGNORECASE)
        if m:
            author = name[m.end():].strip()
            if author and not self._looks_like_title(author):
                return author

        # Pattern 2: "Author - Title" with various dashes/underscore
        for sep in _AUTHOR_SEPARATORS:
            if sep in name:
                potential = name.split(sep, 1)[0].strip()
                word_count = len(potential.split())
                if (
                    potential
                    and 2 <= word_count <= 4
                    and len(potential) < 40
                    and not self._looks_like_title(potential)
                ):
                    return potential

        # Pattern 3: "Title (Author)"
        open_idx = name.rfind("(")
        close_idx = name.rfind(")")
        if open_idx != -1 and close_idx != -1 and open_idx < close_idx:
            author = name[open_idx + 1:close_idx].strip()
            if author and not self._looks_like_title(author):
                return author

        return None

    def _looks_like_title(self, text: str) -> bool:
        normalized = text.lower().replace("_", " ").replace("  ", " ").strip()
        title_lower = self.title.lower()

        if normalized in title_lower or title_lower in normalized:
            return True

        text_words = set(normalized.split())
        title_words = set(title_lower.split())
        overlap = text_words & title_words
        if text_words and len(overlap) / len(text_words) >= 0.5:
            return True

        first = next(iter(text_words), None)
        return first in _COMMON_TITLE_WORDS if first else False


# --------------------------------------------------------------------------- #
# Bookmark marker filter (port of PocketBookNoteResponse.isBookmarkMarker)     #
# --------------------------------------------------------------------------- #

_BOOKMARK_MARKERS = {"bookmark", "bookmarks", "pencil", "note", "notes", "marker"}


def is_bookmark_marker(text: str) -> bool:
    """True when text is a bookmark/annotation marker, not real highlighted content."""
    normalized = text.lower().strip()
    if normalized in _BOOKMARK_MARKERS:
        return True
    # Handle repetition like "Bookmark Bookmark Bookmark"
    words = set(normalized.split())
    if len(words) == 1 and next(iter(words)) in _BOOKMARK_MARKERS:
        return True
    return False


# --------------------------------------------------------------------------- #
# Formatter output                                                             #
# --------------------------------------------------------------------------- #


class BookMarkdown(BaseModel):
    """Result of formatting a book's highlights for the target system."""

    title: str
    description: str | None
    markdown: str
    tags: list[str]
    highlight_count: int


# --------------------------------------------------------------------------- #
# Sync result                                                                  #
# --------------------------------------------------------------------------- #


class SyncResult(BaseModel):
    total_books: int = 0
    total_highlights: int = 0
    skipped_highlights: int = 0
    errors: list[str] = Field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)
