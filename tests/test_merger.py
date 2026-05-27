from pb2craft.processing.merger import HighlightMerger, MergerConfig

from tests.conftest import make_highlight, utc


def test_returns_input_when_single_highlight():
    merger = HighlightMerger()
    [h] = merger.merge([make_highlight(id="solo")])
    assert h.id == "solo"


def test_merges_two_adjacent_same_color_highlights():
    """Classic case: split across a page break, same color, no sentence terminator."""
    first = make_highlight(
        id="h1",
        text="the cat sat on",
        color="yellow",
        begin="epubcfi(/6/14!/4/2/1:0)",
        end="epubcfi(/6/14!/4/2/1:14)",
        created=utc(2024, 1, 1, 10, 0, 0),
    )
    second = make_highlight(
        id="h2",
        text="the mat",
        color="yellow",
        begin="epubcfi(/6/14!/4/2/1:15)",
        end="epubcfi(/6/14!/4/2/1:22)",
        created=utc(2024, 1, 1, 10, 0, 30),
    )

    merger = HighlightMerger()
    result = merger.merge([first, second])
    assert len(result) == 1
    assert result[0].text == "the cat sat on the mat"
    assert result[0].id == "h1+h2"


def test_does_not_merge_different_colors():
    a = make_highlight(id="a", text="alpha", color="yellow")
    b = make_highlight(id="b", text="beta", color="red")
    merger = HighlightMerger()
    result = merger.merge([a, b])
    assert len(result) == 2


def test_does_not_merge_when_first_ends_with_sentence_terminator():
    """A terminator at the end of the first highlight means they're separate sentences."""
    first = make_highlight(
        id="h1",
        text="End of thought.",
        color="yellow",
        begin="epubcfi(/6/14!/4/2/1:0)",
        end="epubcfi(/6/14!/4/2/1:14)",
        created=utc(2024, 1, 1, 10, 0, 0),
    )
    second = make_highlight(
        id="h2",
        text="New sentence",
        color="yellow",
        begin="epubcfi(/6/14!/4/2/1:15)",
        end="epubcfi(/6/14!/4/2/1:27)",
        created=utc(2024, 1, 1, 10, 0, 1),
    )
    merger = HighlightMerger()
    assert len(merger.merge([first, second])) == 2


def test_does_not_merge_when_too_far_apart_in_time():
    cfg = MergerConfig(time_threshold=5.0)  # 5 second window
    first = make_highlight(
        id="h1", text="left", color="yellow",
        begin="epubcfi(/6/14!/4/2/1:0)", end="epubcfi(/6/14!/4/2/1:4)",
        created=utc(2024, 1, 1, 10, 0, 0),
    )
    second = make_highlight(
        id="h2", text="right", color="yellow",
        begin="epubcfi(/6/14!/4/2/1:5)", end="epubcfi(/6/14!/4/2/1:10)",
        created=utc(2024, 1, 1, 10, 5, 0),  # 5 minutes later
    )
    merger = HighlightMerger(config=cfg)
    assert len(merger.merge([first, second])) == 2


def test_combines_notes_when_merging():
    first = make_highlight(
        id="h1", text="alpha", color="yellow", note="first note",
        begin="epubcfi(/6/14!/4/2/1:0)", end="epubcfi(/6/14!/4/2/1:5)",
        created=utc(2024, 1, 1, 10, 0, 0),
    )
    second = make_highlight(
        id="h2", text="beta", color="yellow", note="second note",
        begin="epubcfi(/6/14!/4/2/1:6)", end="epubcfi(/6/14!/4/2/1:10)",
        created=utc(2024, 1, 1, 10, 0, 10),
    )
    merger = HighlightMerger()
    [merged] = merger.merge([first, second])
    assert merged.note == "first note\nsecond note"


def test_merge_with_stats():
    a = make_highlight(
        id="a", text="alpha", color="yellow",
        begin="epubcfi(/6/14!/4/2/1:0)", end="epubcfi(/6/14!/4/2/1:5)",
        created=utc(2024, 1, 1, 10, 0, 0),
    )
    b = make_highlight(
        id="b", text="beta", color="yellow",
        begin="epubcfi(/6/14!/4/2/1:6)", end="epubcfi(/6/14!/4/2/1:10)",
        created=utc(2024, 1, 1, 10, 0, 10),
    )
    merger = HighlightMerger()
    stats = merger.merge_with_stats([a, b])
    assert stats.original_count == 2
    assert stats.merged_count == 1
    assert stats.had_merges
    assert stats.reduction_count == 1
