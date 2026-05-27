"""Highlight merger — reassembles split multi-page highlights.

A single user highlight that spans a page break in PocketBook arrives as
multiple API entries. We rejoin them when:

  * same color
  * CFI end-of-first ↔ begin-of-second is within ``cfi_threshold``
  * first highlight does NOT end with a sentence terminator (continuation hint)
  * creation timestamps are within ``time_threshold`` seconds (if both present)

Port of HighlightMerger.swift.
"""

from __future__ import annotations

from dataclasses import dataclass

from pb2craft.models import Highlight, Quotation
from pb2craft.processing import cfi, sorter


@dataclass(frozen=True)
class MergerConfig:
    cfi_threshold: float = 100.0
    time_threshold: float = 60.0  # seconds


@dataclass(frozen=True)
class MergeResult:
    highlights: list[Highlight]
    original_count: int
    merged_count: int

    @property
    def reduction_count(self) -> int:
        return self.original_count - self.merged_count

    @property
    def had_merges(self) -> bool:
        return self.reduction_count > 0


class HighlightMerger:
    def __init__(self, config: MergerConfig | None = None):
        self.config = config or MergerConfig()

    # ----- public ----------------------------------------------------------- #

    def merge(self, highlights: list[Highlight]) -> list[Highlight]:
        if len(highlights) <= 1:
            return list(highlights)

        sorted_highlights = sorter.sort(highlights)
        color_groups = sorter.group_by_color(sorted_highlights)

        merged: list[Highlight] = []
        for color_highlights in color_groups.values():
            merged.extend(self._merge_color_group(color_highlights))

        return sorter.sort(merged)

    def merge_with_stats(self, highlights: list[Highlight]) -> MergeResult:
        merged = self.merge(highlights)
        return MergeResult(
            highlights=merged,
            original_count=len(highlights),
            merged_count=len(merged),
        )

    # ----- internals -------------------------------------------------------- #

    def _merge_color_group(self, highlights: list[Highlight]) -> list[Highlight]:
        if len(highlights) <= 1:
            return list(highlights)

        result: list[Highlight] = []
        current = highlights[0]

        for nxt in highlights[1:]:
            if self.should_merge(current, nxt):
                current = self._merge_highlights(current, nxt)
            else:
                result.append(current)
                current = nxt

        result.append(current)
        return result

    def should_merge(self, first: Highlight, second: Highlight) -> bool:
        if first.color.value != second.color.value:
            return False
        if not self._check_cfi_adjacency(first, second):
            return False
        if not self._check_text_continuity(first, second):
            return False
        if not self._check_time_proximity(first, second):
            return False
        return True

    def _check_cfi_adjacency(self, first: Highlight, second: Highlight) -> bool:
        first_end = first.end_position
        second_begin = second.begin_position
        if first_end is None or second_begin is None:
            # If we can't compare CFIs, defer the decision to time-proximity.
            return True
        return cfi.are_adjacent(first_end, second_begin, threshold=self.config.cfi_threshold)

    def _check_text_continuity(self, first: Highlight, _second: Highlight) -> bool:
        # If the first ends with terminator (./!/?/quote), they're separate sentences.
        # We intentionally don't gate on the second's casing (proper nouns mid-sentence).
        return not first.ends_with_sentence_terminator

    def _check_time_proximity(self, first: Highlight, second: Highlight) -> bool:
        first_time = first.created_timestamp
        second_time = second.created_timestamp
        if first_time is None or second_time is None:
            return True
        delta = abs((second_time - first_time).total_seconds())
        return delta <= self.config.time_threshold

    def _merge_highlights(self, first: Highlight, second: Highlight) -> Highlight:
        combined_text = " ".join(t.strip() for t in (first.text, second.text))

        # Combine notes
        if first.note and second.note:
            combined_note: str | None = f"{first.note}\n{second.note}"
        else:
            combined_note = first.note or second.note

        merged_quotation = Quotation(
            begin=first.quotation.begin,
            end=second.quotation.end,
            text=combined_text,
            updated=second.quotation.updated or first.quotation.updated,
        )

        return Highlight(
            id=f"{first.id}+{second.id}",
            uuid=first.uuid,
            book_id=first.book_id,
            book_fast_hash=first.book_fast_hash,
            color=first.color,
            note=combined_note,
            text=combined_text,
            quotation=merged_quotation,
            mark=first.mark,
        )
