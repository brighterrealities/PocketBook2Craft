"""Highlight sorting and grouping — port of HighlightSorter.swift / HighlightGrouper.

Sort priority:
  1. CFI position (most accurate when both highlights have parseable CFIs)
  2. Numeric portion of the mark anchor URL
  3. Creation timestamp
  4. Update timestamp
  5. UUID (stable fallback)
"""

from __future__ import annotations

import re
from collections import defaultdict
from functools import cmp_to_key

from pb2craft.models import Highlight

_DIGIT_RE = re.compile(r"\D+")


def _extract_number(s: str) -> int | None:
    digits = _DIGIT_RE.sub("", s)
    return int(digits) if digits else None


def compare_highlights(a: Highlight, b: Highlight) -> int:
    """Return -1, 0, or 1 — classic cmp semantics."""

    # Strategy 1: CFI begin position
    a_pos = a.begin_position
    b_pos = b.begin_position
    if a_pos is not None and b_pos is not None and a_pos != b_pos:
        return -1 if a_pos < b_pos else 1

    # Strategy 2: anchor numeric prefix
    a_anchor = a.mark.anchor if a.mark else None
    b_anchor = b.mark.anchor if b.mark else None
    if a_anchor and b_anchor:
        a_num = _extract_number(a_anchor)
        b_num = _extract_number(b_anchor)
        if a_num is not None and b_num is not None and a_num != b_num:
            return -1 if a_num < b_num else 1

    # Strategy 3: created timestamp
    a_created = a.mark.created if a.mark else None
    b_created = b.mark.created if b.mark else None
    if a_created and b_created and a_created != b_created:
        return -1 if a_created < b_created else 1

    # Strategy 4: updated timestamp on quotation
    a_updated = a.quotation.updated
    b_updated = b.quotation.updated
    if a_updated and b_updated and a_updated != b_updated:
        return -1 if a_updated < b_updated else 1

    # Strategy 5: uuid (stable)
    if a.uuid == b.uuid:
        return 0
    return -1 if a.uuid < b.uuid else 1


def sort(highlights: list[Highlight]) -> list[Highlight]:
    return sorted(highlights, key=cmp_to_key(compare_highlights))


def group_by_book(highlights: list[Highlight]) -> dict[str, list[Highlight]]:
    groups: dict[str, list[Highlight]] = defaultdict(list)
    for h in highlights:
        groups[h.book_id].append(h)
    return {book_id: sort(hs) for book_id, hs in groups.items()}


def group_by_color(highlights: list[Highlight]) -> dict[str, list[Highlight]]:
    groups: dict[str, list[Highlight]] = defaultdict(list)
    for h in highlights:
        groups[h.color.value].append(h)
    return dict(groups)
