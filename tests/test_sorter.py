from pb2craft.processing import sorter

from tests.conftest import make_highlight, utc


def test_sort_by_cfi_position():
    h_late = make_highlight(id="late", begin="epubcfi(/6/14!/4/2/1:80)", end="epubcfi(/6/14!/4/2/1:90)")
    h_early = make_highlight(id="early", begin="epubcfi(/6/14!/4/2/1:0)", end="epubcfi(/6/14!/4/2/1:10)")
    h_mid = make_highlight(id="mid", begin="epubcfi(/6/14!/4/2/1:40)", end="epubcfi(/6/14!/4/2/1:50)")

    result = sorter.sort([h_late, h_early, h_mid])
    assert [h.id for h in result] == ["early", "mid", "late"]


def test_sort_falls_back_to_anchor_when_cfi_equal():
    # Same CFI; anchor numbers differ
    h_a = make_highlight(id="a", anchor="pbr:/page?page=20")
    h_b = make_highlight(id="b", anchor="pbr:/page?page=5")
    result = sorter.sort([h_a, h_b])
    assert [h.id for h in result] == ["b", "a"]


def test_sort_falls_back_to_created_timestamp():
    # No anchor numeric difference (both same), use timestamps
    early = make_highlight(id="early", anchor=None, created=utc(2024, 1, 1, 10))
    late = make_highlight(id="late", anchor=None, created=utc(2024, 1, 1, 12))
    result = sorter.sort([late, early])
    assert [h.id for h in result] == ["early", "late"]


def test_sort_uuid_stable_fallback():
    a = make_highlight(id="aaa", anchor=None, created=None)
    b = make_highlight(id="bbb", anchor=None, created=None)
    result = sorter.sort([b, a])
    assert [h.id for h in result] == ["aaa", "bbb"]


def test_group_by_book():
    h1 = make_highlight(id="h1", book_id="book-A")
    h2 = make_highlight(id="h2", book_id="book-B")
    h3 = make_highlight(id="h3", book_id="book-A")
    grouped = sorter.group_by_book([h1, h2, h3])
    assert set(grouped.keys()) == {"book-A", "book-B"}
    assert {h.id for h in grouped["book-A"]} == {"h1", "h3"}


def test_group_by_color():
    yellow = make_highlight(id="y", color="yellow")
    red = make_highlight(id="r", color="red")
    grouped = sorter.group_by_color([yellow, red])
    assert grouped["yellow"][0].id == "y"
    assert grouped["red"][0].id == "r"
