"""Tests for the seen_books fast-skip that covers books without highlights."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pb2craft.api.craft import CraftDocument
from pb2craft.models import Book
from pb2craft.state import SyncStateStore
from pb2craft.sync import SyncService


@pytest.fixture
def state(tmp_path):
    return SyncStateStore(tmp_path / "state.sqlite")


# --------------------------------------------------------------------------- #
# State-level                                                                  #
# --------------------------------------------------------------------------- #


def test_seen_mtime_round_trip(state: SyncStateStore):
    assert state.get_seen_mtime("b1") is None
    state.mark_book_seen("b1", "2026-01-01T10:00:00Z")
    assert state.get_seen_mtime("b1") == "2026-01-01T10:00:00Z"


def test_seen_mtime_upserts(state: SyncStateStore):
    state.mark_book_seen("b1", "2026-01-01T10:00:00Z")
    state.mark_book_seen("b1", "2026-02-01T10:00:00Z")
    assert state.get_seen_mtime("b1") == "2026-02-01T10:00:00Z"


def test_reset_clears_seen_books(state: SyncStateStore):
    state.set_folder_id("folder-1")
    state.mark_book_seen("b1", "2026-01-01T10:00:00Z")
    state.reset()
    assert state.get_seen_mtime("b1") is None
    # Folder id preserved
    assert state.get_folder_id() == "folder-1"


# --------------------------------------------------------------------------- #
# Sync-level: books with no highlights                                         #
# --------------------------------------------------------------------------- #


def _mock_craft_empty():
    craft = AsyncMock()
    craft.ensure_folder.return_value = "folder-1"
    craft.list_documents.return_value = []
    craft.create_document.return_value = "doc-new"
    craft.upload_image.return_value = "block"
    craft.insert_blocks.return_value = None
    return craft


async def test_no_highlights_book_gets_marked_seen(state: SyncStateStore):
    """A book without any highlights should still get a seen_books row."""
    book = Book.model_validate({
        "id": "empty-book", "fast_hash": "h", "title": "No Highlights",
        "mtime": "2026-01-01T10:00:00Z",
    })

    pb = AsyncMock()
    pb.get_books.return_value = [book]
    pb.get_highlights.return_value = []  # no highlights at all
    craft = _mock_craft_empty()

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync()

    # /notes was fetched once (to discover the book is empty)
    pb.get_highlights.assert_awaited_once()
    # And the mtime is now recorded for next time
    assert state.get_seen_mtime("empty-book") == "2026-01-01T10:00:00Z"


async def test_second_sync_of_empty_book_skips_notes_call(state: SyncStateStore):
    """The whole point: second sync of an unchanged empty book costs zero per-book calls."""
    book = Book.model_validate({
        "id": "empty-book", "fast_hash": "h", "title": "No Highlights",
        "mtime": "2026-01-01T10:00:00Z",
    })

    pb = AsyncMock()
    pb.get_books.return_value = [book]
    pb.get_highlights.return_value = []
    craft = _mock_craft_empty()

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync()
    assert pb.get_highlights.await_count == 1

    # Second sync: same book, same mtime → skip entirely
    await svc.sync()
    assert pb.get_highlights.await_count == 1  # unchanged


async def test_book_with_changed_mtime_is_re_examined(state: SyncStateStore):
    """If mtime changes between syncs, we re-fetch /notes."""
    book_v1 = Book.model_validate({
        "id": "b1", "fast_hash": "h", "title": "T",
        "mtime": "2026-01-01T10:00:00Z",
    })
    book_v2 = Book.model_validate({
        "id": "b1", "fast_hash": "h", "title": "T",
        "mtime": "2026-02-01T10:00:00Z",  # different mtime
    })

    pb = AsyncMock()
    pb.get_highlights.return_value = []
    pb.get_books.side_effect = [[book_v1], [book_v2]]
    craft = _mock_craft_empty()

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync()
    await svc.sync()

    # Both syncs fetched the book — mtime changed
    assert pb.get_highlights.await_count == 2


async def test_force_resync_clears_seen_books(state: SyncStateStore):
    """Force resync should re-examine every book, even ones marked seen."""
    book = Book.model_validate({
        "id": "b1", "fast_hash": "h", "title": "T",
        "mtime": "2026-01-01T10:00:00Z",
    })

    pb = AsyncMock()
    pb.get_books.return_value = [book]
    pb.get_highlights.return_value = []
    craft = _mock_craft_empty()

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync()
    assert pb.get_highlights.await_count == 1

    # Force resync: state cleared, book re-examined despite unchanged mtime
    await svc.sync(force=True)
    assert pb.get_highlights.await_count == 2
