from datetime import datetime, timezone

from pb2craft import formatter
from pb2craft.models import is_bookmark_marker

from tests.conftest import make_book, make_highlight


def test_color_to_emoji_known_colors():
    assert formatter.color_to_emoji("yellow") == "🟡"
    assert formatter.color_to_emoji("RED") == "🔴"
    assert formatter.color_to_emoji("blue") == "🔵"


def test_color_to_emoji_unknown_falls_back():
    assert formatter.color_to_emoji("magenta") == "📝"


def test_format_book_contains_metadata_and_highlights():
    book = make_book(title="Sample Book", path="/Jane Doe - Sample Book.epub")
    h = make_highlight(text="quoted passage", anchor="pbr:/page?page=42")
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    result = formatter.format_book(book, [h], now=now)

    assert result.title == "Sample Book"
    assert result.highlight_count == 1
    assert "## Book Highlights" in result.markdown
    assert "**Author**: Jane Doe" in result.markdown
    assert "(p. 42)" in result.markdown
    # Quoted text is wrapped in Craft's <highlight color> inline tag (yellow default)
    assert '> <highlight color="yellow">quoted passage</highlight>' in result.markdown
    assert "pocketbook-import" in result.tags


def test_format_book_omits_author_line_when_unknown():
    book = make_book(title="No Author Book", path=None)
    result = formatter.format_book(book, [make_highlight()])
    assert "**Author**:" not in result.markdown


def test_format_book_includes_note_when_present():
    book = make_book()
    h = make_highlight(text="quote", note="my reflection")
    result = formatter.format_book(book, [h])
    assert "*Note: my reflection*" in result.markdown


def test_format_book_tags_include_collection_tags():
    book = make_book(collections="Philosophy, Self Help")
    result = formatter.format_book(book, [make_highlight()])
    assert "philosophy" in result.tags
    assert "self help" in result.tags


def test_book_url_uses_link_when_available():
    book = make_book(link="https://example.com/my-book")
    assert formatter.book_url(book) == "https://example.com/my-book"


def test_book_url_falls_back_to_fast_hash():
    book = make_book(link=None, fast_hash="abc123")
    assert formatter.book_url(book) == "https://cloud.pocketbook.digital/library#book-abc123"


def test_is_bookmark_marker_detects_common_words():
    assert is_bookmark_marker("bookmark")
    assert is_bookmark_marker("Bookmarks")
    assert is_bookmark_marker("pencil")
    assert is_bookmark_marker("Bookmark Bookmark Bookmark")  # repetition


def test_is_bookmark_marker_rejects_real_content():
    assert not is_bookmark_marker("This is a real highlight")
    assert not is_bookmark_marker("bookmark this idea")  # mixed words

def test_format_summary_shows_preview():
    book = make_book(path="/Author - Title.epub")
    highlights = [
        make_highlight(id="h1", text="first highlight " * 5, anchor="pbr:/page?page=3"),
        make_highlight(id="h2", text="second highlight"),
    ]
    summary = formatter.format_summary(book, highlights)
    assert "Highlights: 2" in summary
    assert "(p. 3)" in summary
    assert "first highlight" in summary
