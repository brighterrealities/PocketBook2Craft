"""Tests for the mtime-based fast-path skip in sync."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pb2craft.api.craft import CraftDocument
from pb2craft.models import Book
from pb2craft.state import SyncStateStore
from pb2craft.sync import SyncService

from tests.conftest import make_highlight


# --------------------------------------------------------------------------- #
# Book.mtime parsing                                                           #
# --------------------------------------------------------------------------- #


def test_book_parses_mtime_field():
    book = Book.model_validate({
        "id": "b1", "fast_hash": "h", "title": "T",
        "mtime": "2025-12-30T23:13:51Z",
    })
    assert book.mtime == "2025-12-30T23:13:51Z"


def test_book_mtime_defaults_to_none_when_absent():
    book = Book.model_validate({"id": "b1", "fast_hash": "h", "title": "T"})
    assert book.mtime is None


# --------------------------------------------------------------------------- #
# SyncStateStore mtime round-trip                                              #
# --------------------------------------------------------------------------- #


@pytest.fixture
def state(tmp_path):
    return SyncStateStore(tmp_path / "state.sqlite")


def test_state_persists_last_known_mtime(state: SyncStateStore):
    state.mark_book_synced(
        book_id="b1", craft_doc_id="d1", title="T", highlight_count=2,
        last_known_mtime="2025-12-30T23:13:51Z",
    )
    synced = state.get_book("b1")
    assert synced is not None
    assert synced.last_known_mtime == "2025-12-30T23:13:51Z"


def test_state_mtime_defaults_to_none(state: SyncStateStore):
    """Callers that don't pass mtime get None back."""
    state.mark_book_synced(book_id="b1", craft_doc_id="d1", title="T", highlight_count=0)
    synced = state.get_book("b1")
    assert synced is not None
    assert synced.last_known_mtime is None


# --------------------------------------------------------------------------- #
# Sync skip behavior                                                           #
# --------------------------------------------------------------------------- #


def _mock_pocketbook(books):
    pb = AsyncMock()
    pb.get_books.return_value = books
    pb.get_highlights.return_value = [make_highlight(id="h1", uuid="h1", book_id=books[0].id)]
    return pb


def _mock_craft_with_doc(doc_id: str, title: str):
    craft = AsyncMock()
    craft.ensure_folder.return_value = "folder-1"
    craft.list_documents.return_value = [CraftDocument(id=doc_id, title=title)]
    craft.create_document.return_value = "doc-created"
    craft.upload_image.return_value = "block-cover"
    craft.insert_blocks.return_value = None
    return craft


async def test_sync_skips_fetch_when_mtime_unchanged(state: SyncStateStore):
    """seen_books mtime matches book mtime → no get_highlights call.

    Even the doc-existence check is skipped — the seen_books fast-path runs
    before any other state lookup.
    """
    book = Book.model_validate({
        "id": "b1", "fast_hash": "h", "title": "Some Book",
        "mtime": "2025-12-30T23:13:51Z",
    })

    # Seed seen_books with the SAME mtime — this is the fast-path trigger
    state.set_folder_id("folder-1")
    state.mark_book_seen("b1", "2025-12-30T23:13:51Z")
    state.mark_book_synced(
        book_id="b1", craft_doc_id="doc-1", title="Some Book",
        highlight_count=5, last_known_mtime="2025-12-30T23:13:51Z",
    )
    state.mark_highlights_synced(book_id="b1", highlight_uuids=["h1", "h2"])

    pb = _mock_pocketbook([book])
    craft = _mock_craft_with_doc("doc-1", "Some Book")

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    result = await svc.sync()

    # The fast path skipped get_highlights entirely
    pb.get_highlights.assert_not_awaited()
    craft.insert_blocks.assert_not_awaited()
    craft.append_to_book_document.assert_not_awaited()
    assert result.total_highlights == 0
    assert not result.has_errors


async def test_sync_fetches_when_mtime_differs(state: SyncStateStore):
    """Recorded mtime differs → must fetch to compare highlights."""
    book = Book.model_validate({
        "id": "b1", "fast_hash": "h", "title": "Some Book",
        "mtime": "2026-01-15T10:00:00Z",  # new mtime
    })

    state.set_folder_id("folder-1")
    state.mark_book_synced(
        book_id="b1", craft_doc_id="doc-1", title="Some Book",
        highlight_count=1, last_known_mtime="2025-12-30T23:13:51Z",  # old mtime
    )
    state.mark_highlights_synced(book_id="b1", highlight_uuids=["h1"])

    pb = _mock_pocketbook([book])
    craft = _mock_craft_with_doc("doc-1", "Some Book")

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync()

    pb.get_highlights.assert_awaited_once()


async def test_sync_fetches_when_stored_mtime_is_none(state: SyncStateStore):
    """Pre-migration row with NULL mtime → don't skip, fetch as normal."""
    book = Book.model_validate({
        "id": "b1", "fast_hash": "h", "title": "Some Book",
        "mtime": "2026-01-15T10:00:00Z",
    })

    state.set_folder_id("folder-1")
    state.mark_book_synced(
        book_id="b1", craft_doc_id="doc-1", title="Some Book",
        highlight_count=1,  # last_known_mtime defaults to None
    )
    state.mark_highlights_synced(book_id="b1", highlight_uuids=["h1"])

    pb = _mock_pocketbook([book])
    craft = _mock_craft_with_doc("doc-1", "Some Book")

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync()

    pb.get_highlights.assert_awaited_once()


async def test_sync_fetches_when_book_mtime_missing(state: SyncStateStore):
    """Book has no mtime field at all → can't gate, fetch as normal."""
    book = Book.model_validate({
        "id": "b1", "fast_hash": "h", "title": "Some Book",
        # no mtime
    })

    state.set_folder_id("folder-1")
    state.mark_book_synced(
        book_id="b1", craft_doc_id="doc-1", title="Some Book",
        highlight_count=1, last_known_mtime="2025-12-30T23:13:51Z",
    )
    state.mark_highlights_synced(book_id="b1", highlight_uuids=["h1"])

    pb = _mock_pocketbook([book])
    craft = _mock_craft_with_doc("doc-1", "Some Book")

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync()

    pb.get_highlights.assert_awaited_once()


async def test_first_time_sync_records_mtime(state: SyncStateStore):
    """A fresh sync stores the book's current mtime for later gating."""
    book = Book.model_validate({
        "id": "b1", "fast_hash": "h", "title": "Some Book",
        "mtime": "2026-01-15T10:00:00Z",
    })

    pb = _mock_pocketbook([book])
    pb.get_highlights.return_value = [make_highlight(id="h1", uuid="h1", book_id="b1")]
    craft = AsyncMock()
    craft.ensure_folder.return_value = "folder-1"
    craft.list_documents.return_value = []  # empty folder
    craft.create_document.return_value = "doc-new"
    craft.upload_image.return_value = "block-cover"
    craft.insert_blocks.return_value = None

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync()

    synced = state.get_book("b1")
    assert synced is not None
    assert synced.last_known_mtime == "2026-01-15T10:00:00Z"
