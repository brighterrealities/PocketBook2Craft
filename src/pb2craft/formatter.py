"""Markdown formatter — turns a book + highlights into Craft-bound output.

The sync hot path uses :func:`format_book_blocks` and
:func:`format_highlight_blocks`, which return typed Craft block payloads
(``page``-as-``card`` for each highlight, with inline ``<highlight>`` color
tags). The older :func:`format_book` / :func:`to_craft_blocks` helpers are
kept as a lower-fidelity preview / fallback path.
"""

from __future__ import annotations

import re
from datetime import datetime
from html import escape as _xml_escape

from pb2craft.models import Book, BookMarkdown, Highlight
from pb2craft.processing import sorter

_COLOR_EMOJI = {
    "yellow": "🟡",
    "red": "🔴",
    "green": "🟢",
    "blue": "🔵",
    "purple": "🟣",
    "orange": "🟠",
}
_DEFAULT_COLOR_EMOJI = "📝"

# PocketBook highlight color → Craft <highlight color="…"> value.
# Direct matches for five of six PB colors; "orange" has no equivalent in
# Craft's palette so we fall back to gradient-yellow as the closest visual.
# Unknown / uncolored highlights map to None (rendered without a color wrap).
_PB_TO_CRAFT_COLOR = {
    "yellow": "yellow",
    "green": "green",
    "blue": "blue",
    "purple": "purple",
    "red": "red",
    "orange": "gradient-yellow",
}


def craft_color_for(pb_color: str) -> str | None:
    """Map a PocketBook highlight color to a Craft `<highlight color>` value."""
    return _PB_TO_CRAFT_COLOR.get((pb_color or "").lower())


# Visual separator between highlight blocks in the rendered markdown.
HIGHLIGHT_SEPARATOR = "---"


def color_to_emoji(color: str) -> str:
    return _COLOR_EMOJI.get(color.lower(), _DEFAULT_COLOR_EMOJI)


def format_highlight(highlight: Highlight, number: int) -> str:
    """Render a highlight as markdown with an inline ``<highlight>`` color tag.

    The blockquote ``>`` prefix is preserved per line; each line's content is
    XML-escaped and wrapped in ``<highlight color="…">…</highlight>`` when the
    PocketBook color maps to a Craft color. Unknown / uncolored highlights are
    left as bare escaped text.
    """
    page = highlight.mark.page if highlight.mark else None
    header = f"### Highlight {number}"
    if page is not None:
        header += f" (p. {page})"

    color = craft_color_for(highlight.color.value)

    quote_lines: list[str] = []
    for line in highlight.text.split("\n"):
        escaped = _xml_escape(line)
        wrapped = f"<highlight color=\"{color}\">{escaped}</highlight>" if color else escaped
        quote_lines.append(f"> {wrapped}")
    quoted = "\n".join(quote_lines)

    parts = [header, "", quoted]
    if highlight.has_note:
        parts += ["", f"*Note: {_xml_escape(highlight.note or '')}*"]
    return "\n".join(parts)


def format_book(book: Book, highlights: list[Highlight], *, now: datetime | None = None) -> BookMarkdown:
    """Produce markdown + metadata for a single book."""
    sorted_highlights = sorter.sort(highlights)
    timestamp = now or datetime.now()

    lines: list[str] = ["## Book Highlights", ""]

    if book.display_authors != "Unknown Author":
        lines.append(f"**Author**: {book.display_authors}")

    # Optional extended metadata — skip nulls so the block stays clean.
    md = book.metadata
    if md is not None:
        if md.publisher:
            lines.append(f"**Publisher**: {md.publisher}")
        if md.year:
            lines.append(f"**Year**: {md.year}")
        if md.isbn:
            lines.append(f"**ISBN**: {md.isbn}")
        if md.lang:
            lines.append(f"**Language**: {md.lang}")

    # Trailing blank between metadata and synced timestamp
    if len(lines) > 2:
        lines.append("")

    lines += [f"**Synced**: {timestamp.strftime('%b %d, %Y at %I:%M %p')}", "", HIGHLIGHT_SEPARATOR, ""]

    for i, h in enumerate(sorted_highlights, start=1):
        lines.append(format_highlight(h, i))
        lines += ["", HIGHLIGHT_SEPARATOR, ""]

    markdown = "\n".join(lines)

    description = _description_from_path(book)
    tags = _tags_for_book(book)

    return BookMarkdown(
        title=book.display_title,
        description=description,
        markdown=markdown,
        tags=tags,
        highlight_count=len(sorted_highlights),
    )


def _description_from_path(book: Book) -> str | None:
    if not book.path:
        return None
    filename = book.path.rsplit("/", 1)[-1]
    if " - " in filename:
        return filename.split("-", 1)[0].strip()
    return None


def _tags_for_book(book: Book) -> list[str]:
    tags: list[str] = ["pocketbook-import"]
    if book.collections:
        # Collections come in as a comma-separated string from the API.
        collection_tags = [c.strip().lower() for c in book.collections.split(",") if c.strip()]
        tags.extend(collection_tags[:29])  # leave room for the import tag
    return tags


# --------------------------------------------------------------------------- #
# URL helpers                                                                  #
# --------------------------------------------------------------------------- #


def book_url(book: Book) -> str:
    if book.link:
        return book.link
    return f"https://cloud.pocketbook.digital/library#book-{book.fast_hash}"


def highlight_url(highlight: Highlight) -> str:
    return f"https://cloud.pocketbook.digital/library#highlight-{highlight.uuid}"


# --------------------------------------------------------------------------- #
# Text-block formatter with explicit decorations (sync hot path)               #
# --------------------------------------------------------------------------- #


def format_book_text_blocks(
    book: Book,
    highlights: list[Highlight],
    *,
    decorations: list[str] | None = None,
    add_author_tag: bool = False,
    add_publisher_tag: bool = False,
    now: datetime | None = None,
) -> list[dict]:
    """Build text blocks for a fresh book doc, with explicit per-quote decorations.

    Output: [metadata_text_block, (h3_heading_block + quote_block)…]
    Each quote block carries the ``decorations`` field so Craft renders the
    chosen style ("quote" = Focus, "callout" = Block, both stacked, or none).
    """
    sorted_highlights = sorter.sort(highlights)
    blocks: list[dict] = [
        _header_text_block(
            book,
            now=now,
            add_author_tag=add_author_tag,
            add_publisher_tag=add_publisher_tag,
        )
    ]
    for index, h in enumerate(sorted_highlights, start=1):
        blocks.extend(_highlight_text_blocks(h, index, decorations=decorations))
    return blocks


def format_highlight_text_blocks(
    highlights: list[Highlight],
    *,
    decorations: list[str] | None = None,
    start_index: int = 1,
) -> list[dict]:
    """Build text blocks for an incremental append.

    Tags only apply to the first-time path (header block) — appends are
    pure highlight content.
    """
    sorted_highlights = sorter.sort(highlights)
    out: list[dict] = []
    for offset, h in enumerate(sorted_highlights):
        out.extend(_highlight_text_blocks(h, start_index + offset, decorations=decorations))
    return out


def _header_text_block(
    book: Book,
    *,
    now: datetime | None,
    add_author_tag: bool = False,
    add_publisher_tag: bool = False,
) -> dict:
    timestamp = now or datetime.now()
    lines: list[str] = ["## Book Highlights", ""]
    if book.display_authors != "Unknown Author":
        lines.append(f"**Author**: {book.display_authors}")
    md = book.metadata
    if md is not None:
        if md.publisher:
            lines.append(f"**Publisher**: {md.publisher}")
        if md.year:
            lines.append(f"**Year**: {md.year}")
        if md.isbn:
            lines.append(f"**ISBN**: {md.isbn}")
        if md.lang:
            lines.append(f"**Language**: {md.lang}")
    if len(lines) > 2:
        lines.append("")
    lines.append(f"**Synced**: {timestamp.strftime('%b %d, %Y at %I:%M %p')}")

    tags: list[str] = []
    if add_author_tag and book.display_authors != "Unknown Author":
        tags.extend(author_tags(book.display_authors))
    if add_publisher_tag and md is not None and md.publisher:
        publisher_tag_value = publisher_tag(md.publisher)
        if publisher_tag_value:
            tags.append(publisher_tag_value)
    if tags:
        lines += ["", " ".join(tags)]

    return {"type": "text", "markdown": "\n".join(lines)}


# --------------------------------------------------------------------------- #
# Tag helpers                                                                  #
# --------------------------------------------------------------------------- #


# Split author strings on common separators ("&", "and", ","); each remaining
# part becomes its own tag.
_AUTHOR_SEPARATORS = re.compile(r"\s*(?:,|&| and )\s*", re.IGNORECASE)

# Craft tags terminate at the first non-word character (periods, hyphens,
# ampersands, etc.), so we strip all of those — not just whitespace. ``\w`` is
# Unicode-aware in Python 3, so accented letters like "François" survive.
_TAG_STRIP = re.compile(r"\W+", re.UNICODE)


def author_tags(authors: str) -> list[str]:
    """Split a multi-author string into a list of ``#FirstLast``-style tags.

    Separators: comma, ampersand, ``" and "``. Inside each name part, every
    non-word character is dropped (so "Elaine N. Aron" → ``#ElaineNAron``) —
    otherwise Craft truncates the tag at the first ``.`` or hyphen.
    """
    out: list[str] = []
    for part in _AUTHOR_SEPARATORS.split(authors or ""):
        cleaned = _TAG_STRIP.sub("", part)
        if cleaned:
            out.append(f"#{cleaned}")
    return out


def publisher_tag(publisher: str) -> str | None:
    """Non-word-stripped, ``#``-prefixed publisher tag. None if empty."""
    cleaned = _TAG_STRIP.sub("", publisher or "")
    return f"#{cleaned}" if cleaned else None


def _highlight_text_blocks(
    h: Highlight, number: int, *, decorations: list[str] | None
) -> list[dict]:
    # Heading block — plain h3, no decoration.
    page = h.mark.page if h.mark else None
    title = f"Highlight {number}"
    if page is not None:
        title += f" (p. {page})"
    heading = {
        "type": "text",
        "textStyle": "h3",
        "markdown": f"### {_xml_escape(title)}",
    }

    # Quote block — content wrapped in inline color tag, decorations set explicitly.
    color = craft_color_for(h.color.value)
    quote_lines: list[str] = []
    for line in h.text.split("\n"):
        escaped = _xml_escape(line)
        wrapped = f"<highlight color=\"{color}\">{escaped}</highlight>" if color else escaped
        quote_lines.append(wrapped)
    if h.has_note:
        quote_lines.append("")
        quote_lines.append(f"*Note: {_xml_escape(h.note or '')}*")
    quote_block: dict = {
        "type": "text",
        "markdown": "\n".join(quote_lines),
    }
    if decorations:
        quote_block["decorations"] = list(decorations)
    return [heading, quote_block]


# --------------------------------------------------------------------------- #
# Card-block formatter (kept for a future iteration; not used by sync)         #
# --------------------------------------------------------------------------- #


def format_book_blocks(
    book: Book,
    highlights: list[Highlight],
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Build Craft block payloads for a fresh book document.

    Returns ``[header_text_block, card_per_highlight…]``. Highlights are sorted
    by position in the book. Each card uses Craft's ``page``/``textStyle=card``
    block with inline ``<highlight color>`` tags for the quoted text.
    """
    sorted_highlights = sorter.sort(highlights)
    blocks: list[dict] = [_header_block(book, now=now)]
    for index, h in enumerate(sorted_highlights, start=1):
        blocks.append(_highlight_card_block(h, index))
    return blocks


def format_highlight_blocks(
    highlights: list[Highlight],
    *,
    start_index: int = 1,
) -> list[dict]:
    """Build card blocks for an incremental append.

    Caller passes ``start_index`` so card titles continue from the existing
    document's highlight count (e.g. start at 6 if the doc already has 5).
    """
    sorted_highlights = sorter.sort(highlights)
    return [
        _highlight_card_block(h, start_index + offset)
        for offset, h in enumerate(sorted_highlights)
    ]


# ----- internal block builders --------------------------------------------- #


def _header_block(book: Book, *, now: datetime | None) -> dict:
    timestamp = now or datetime.now()
    lines: list[str] = ["## Book Highlights", ""]
    if book.display_authors != "Unknown Author":
        lines.append(f"**Author**: {book.display_authors}")
    md = book.metadata
    if md is not None:
        if md.publisher:
            lines.append(f"**Publisher**: {md.publisher}")
        if md.year:
            lines.append(f"**Year**: {md.year}")
        if md.isbn:
            lines.append(f"**ISBN**: {md.isbn}")
        if md.lang:
            lines.append(f"**Language**: {md.lang}")
    if len(lines) > 2:
        lines.append("")
    lines.append(f"**Synced**: {timestamp.strftime('%b %d, %Y at %I:%M %p')}")
    return {"type": "text", "markdown": "\n".join(lines)}


def _highlight_card_block(h: Highlight, number: int) -> dict:
    page = h.mark.page if h.mark else None
    title = f"Highlight {number}"
    if page is not None:
        title += f" (p. {page})"

    quote_inner = _xml_escape(h.text)
    color = craft_color_for(h.color.value)
    if color:
        quoted = f"<highlight color=\"{color}\">{quote_inner}</highlight>"
    else:
        quoted = quote_inner

    content_lines = [quoted]
    if h.has_note:
        content_lines.append("")
        content_lines.append(f"*Note: {_xml_escape(h.note or '')}*")

    content = "\n".join(content_lines)
    markdown = (
        f"<page textStyle=\"card\">"
        f"<pageTitle>{_xml_escape(title)}</pageTitle>"
        f"<content>{content}</content>"
        f"</page>"
    )
    return {"type": "page", "textStyle": "card", "markdown": markdown}


# --------------------------------------------------------------------------- #
# Legacy markdown-blob helpers (kept for preview / fallback use)               #
# --------------------------------------------------------------------------- #


def to_craft_blocks(markdown: str) -> list[dict]:
    """Split a formatted markdown blob into Craft block payloads.

    Splits on the same ``---`` separator that :func:`format_book` inserts, so
    the book metadata becomes one block and each highlight becomes its own.
    Each block is shaped as ``{"type": "text", "markdown": "..."}`` — ready for
    Craft's ``POST /blocks``.
    """
    parts = [chunk.strip() for chunk in markdown.split(f"\n{HIGHLIGHT_SEPARATOR}\n")]
    return [{"type": "text", "markdown": chunk} for chunk in parts if chunk]


def format_highlights_only(highlights: list[Highlight], *, start_index: int = 1) -> str:
    """Format just the highlight list (no book header), separator-joined.

    Used for incremental appends — caller passes ``start_index`` so numbering
    continues from the existing document's highlight count.
    """
    sorted_highlights = sorter.sort(highlights)
    parts: list[str] = []
    for offset, h in enumerate(sorted_highlights):
        parts.append(format_highlight(h, start_index + offset))
    return f"\n{HIGHLIGHT_SEPARATOR}\n".join(parts)


# --------------------------------------------------------------------------- #
# Dry-run summary                                                              #
# --------------------------------------------------------------------------- #


def format_summary(book: Book, highlights: list[Highlight]) -> str:
    lines = [
        f"📖 {book.display_title}",
        f"   Author: {book.display_authors}",
        f"   Highlights: {len(highlights)}",
    ]
    if highlights:
        lines.append("   Preview:")
        for i, h in enumerate(highlights[:3], start=1):
            preview = h.text[:50]
            suffix = "..." if len(h.text) > 50 else ""
            page = h.mark.page if h.mark else None
            page_info = f" (p. {page})" if page is not None else ""
            lines.append(f'     {i}.{page_info} "{preview}{suffix}"')
        if len(highlights) > 3:
            lines.append(f"     ... and {len(highlights) - 3} more")
    return "\n".join(lines)
