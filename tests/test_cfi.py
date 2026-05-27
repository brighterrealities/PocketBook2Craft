from pb2craft.processing import cfi


def test_parse_simple_cfi():
    pos = cfi.parse("epubcfi(/6/14!/4/2/1:42)")
    assert pos is not None
    assert [c.index for c in pos.spine_components] == [6, 14]
    assert [c.index for c in pos.document_path] == [4, 2, 1]
    assert pos.character_offset == 42


def test_parse_with_bracket_id():
    pos = cfi.parse("epubcfi(/6/14!/4/2[chapter1]/1:0)")
    assert pos is not None
    assert pos.document_path[0].index == 4
    assert pos.document_path[1].id == "chapter1"


def test_parse_returns_none_for_empty():
    assert cfi.parse("") is None
    assert cfi.parse("garbage") is None


def test_position_ordering_by_spine():
    a = cfi.parse("epubcfi(/6/14!/4/2/1:0)")
    b = cfi.parse("epubcfi(/6/16!/4/2/1:0)")
    assert a < b
    assert a != b


def test_position_ordering_by_document_path():
    a = cfi.parse("epubcfi(/6/14!/4/2/1:0)")
    b = cfi.parse("epubcfi(/6/14!/4/2/3:0)")
    assert a < b


def test_position_ordering_by_offset():
    a = cfi.parse("epubcfi(/6/14!/4/2/1:5)")
    b = cfi.parse("epubcfi(/6/14!/4/2/1:50)")
    assert a < b


def test_distance_is_zero_for_equal():
    a = cfi.parse("epubcfi(/6/14!/4/2/1:5)")
    assert cfi.distance(a, a) == 0


def test_adjacency_within_threshold():
    a = cfi.parse("epubcfi(/6/14!/4/2/1:0)")
    b = cfi.parse("epubcfi(/6/14!/4/2/1:8)")
    assert cfi.are_adjacent(a, b, threshold=10)


def test_adjacency_outside_threshold():
    a = cfi.parse("epubcfi(/6/14!/4/2/1:0)")
    b = cfi.parse("epubcfi(/6/18!/4/2/1:0)")
    assert not cfi.are_adjacent(a, b, threshold=10)
