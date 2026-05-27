"""Tests for SyncStateStore (SQLite-backed)."""

from __future__ import annotations

import pytest

from pb2craft.state import SyncStateStore

from tests.conftest import make_highlight


@pytest.fixture
def store(tmp_path):
    return SyncStateStore(tmp_path / "state.sqlite")


def test_get_book_missing(store: SyncStateStore):
    assert store.get_book("book-1") is None


def test_mark_book_synced_upserts(store: SyncStateStore):
    store.mark_book_synced(
        book_id="book-1",
        craft_doc_id="doc-1",
        title="Some Book",
        highlight_count=5,
    )
    b = store.get_book("book-1")
    assert b is not None
    assert b.craft_doc_id == "doc-1"
    assert b.highlight_count == 5

    # Upsert path: same book_id, different details
    store.mark_book_synced(
        book_id="book-1",
        craft_doc_id="doc-1",
        title="Some Book",
        highlight_count=8,
    )
    assert store.get_book("book-1").highlight_count == 8  # type: ignore[union-attr]


def test_highlight_tracking(store: SyncStateStore):
    store.mark_highlights_synced(book_id="book-1", highlight_uuids=["a", "b", "c"])
    assert store.get_synced_highlight_ids("book-1") == {"a", "b", "c"}

    # Re-mark is idempotent
    store.mark_highlights_synced(book_id="book-1", highlight_uuids=["c", "d"])
    assert store.get_synced_highlight_ids("book-1") == {"a", "b", "c", "d"}


def test_filter_unsynced(store: SyncStateStore):
    h1 = make_highlight(id="h1", uuid="h1")
    h2 = make_highlight(id="h2", uuid="h2")
    h3 = make_highlight(id="h3", uuid="h3")

    store.mark_highlights_synced(book_id="book-1", highlight_uuids=["h1", "h2"])

    unsynced = store.filter_unsynced(book_id="book-1", highlights=[h1, h2, h3])
    assert [h.uuid for h in unsynced] == ["h3"]


def test_folder_id_round_trip(store: SyncStateStore):
    assert store.get_folder_id() is None
    store.set_folder_id("f-123")
    assert store.get_folder_id() == "f-123"


def test_last_sync_at(store: SyncStateStore):
    assert store.get_last_sync_at() is None
    store.mark_sync_complete()
    ts = store.get_last_sync_at()
    assert ts is not None


def test_reset_clears_state_but_keeps_folder(store: SyncStateStore):
    store.set_folder_id("f-keep")
    store.mark_book_synced(book_id="b", craft_doc_id="d", title="t", highlight_count=1)
    store.mark_highlights_synced(book_id="b", highlight_uuids=["h"])

    store.reset()

    assert store.get_book("b") is None
    assert store.get_synced_highlight_ids("b") == set()
    # folder_id is intentionally preserved
    assert store.get_folder_id() == "f-keep"


def test_summary(store: SyncStateStore):
    store.mark_book_synced(book_id="b1", craft_doc_id="d1", title="t1", highlight_count=2)
    store.mark_book_synced(book_id="b2", craft_doc_id="d2", title="t2", highlight_count=3)
    store.mark_highlights_synced(book_id="b1", highlight_uuids=["a", "b"])
    store.mark_highlights_synced(book_id="b2", highlight_uuids=["c", "d", "e"])

    summary = store.summary()
    assert summary["books"] == 2
    assert summary["highlights"] == 5
