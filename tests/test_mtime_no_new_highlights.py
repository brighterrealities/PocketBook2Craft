"""Regression: incremental sync with no new highlights must still record mtime
so the next sync's fast-path can skip the fetch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pb2craft.api.craft import CraftDocument
from pb2craft.models import Book
from pb2craft.state import SyncStateStore
from pb2craft.sync import SyncService

from tests.conftest import make_highlight


@pytest.fixture
def state(tmp_path):
    return SyncStateStore(tmp_path / "state.sqlite")


async def test_incremental_with_no_new_highlights_records_mtime(state: SyncStateStore):
    """Pre-migration state row has NULL mtime; first sync after upgrade
    must populate mtime so the second sync can skip."""
    book = Book.model_validate({
        "id": "b1", "fast_hash": "h", "title": "Some Book",
        "mtime": "2026-01-15T10:00:00Z",
    })

    state.set_folder_id("folder-1")
    state.mark_book_synced(
        book_id="b1", craft_doc_id="doc-1", title="Some Book",
        highlight_count=1,
        # last_known_mtime intentionally left None (simulates pre-migration row)
    )
    state.mark_highlights_synced(book_id="b1", highlight_uuids=["h1"])

    pb = AsyncMock()
    pb.get_books.return_value = [book]
    pb.get_highlights.return_value = [make_highlight(id="h1", uuid="h1", book_id="b1")]

    craft = AsyncMock()
    craft.ensure_folder.return_value = "folder-1"
    craft.list_documents.return_value = [CraftDocument(id="doc-1", title="Some Book")]
    craft.upload_image.return_value = "block"
    craft.insert_blocks.return_value = None
    craft.append_to_book_document.return_value = None

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync()

    # No new highlights → no append
    craft.append_to_book_document.assert_not_awaited()
    # But mtime IS now recorded so the next sync can skip
    synced = state.get_book("b1")
    assert synced is not None
    assert synced.last_known_mtime == "2026-01-15T10:00:00Z"


async def test_second_sync_after_first_now_skips(state: SyncStateStore):
    """End-to-end: two consecutive syncs, second one should skip the fetch."""
    book = Book.model_validate({
        "id": "b1", "fast_hash": "h", "title": "Some Book",
        "mtime": "2026-01-15T10:00:00Z",
    })

    state.set_folder_id("folder-1")
    state.mark_book_synced(
        book_id="b1", craft_doc_id="doc-1", title="Some Book", highlight_count=1,
        # NULL mtime — pre-migration
    )
    state.mark_highlights_synced(book_id="b1", highlight_uuids=["h1"])

    pb = AsyncMock()
    pb.get_books.return_value = [book]
    pb.get_highlights.return_value = [make_highlight(id="h1", uuid="h1", book_id="b1")]

    craft = AsyncMock()
    craft.ensure_folder.return_value = "folder-1"
    craft.list_documents.return_value = [CraftDocument(id="doc-1", title="Some Book")]
    craft.upload_image.return_value = "block"
    craft.insert_blocks.return_value = None
    craft.append_to_book_document.return_value = None

    svc = SyncService(pocketbook=pb, craft=craft, state=state)

    # First sync: fetches (mtime in state is NULL → no skip)
    await svc.sync()
    assert pb.get_highlights.await_count == 1

    # Second sync: should skip the fetch because mtime now matches
    await svc.sync()
    assert pb.get_highlights.await_count == 1  # still 1, not 2
