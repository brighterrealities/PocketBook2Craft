"""SyncService end-to-end tests with mocked PocketBook + Craft clients."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pb2craft.api.craft import CraftError
from pb2craft.api.pocketbook import PocketBookError
from pb2craft.state import SyncStateStore
from pb2craft.sync import SyncService

from tests.conftest import make_book, make_highlight, utc


@pytest.fixture
def state(tmp_path):
    return SyncStateStore(tmp_path / "state.sqlite")


def _mock_pocketbook(books, highlights_by_book):
    pb = AsyncMock()
    pb.get_books.return_value = books
    async def get_highlights(book):
        return highlights_by_book.get(book.id, [])
    pb.get_highlights.side_effect = get_highlights
    return pb


def _mock_craft(*, existing_docs: list[tuple[str, str]] | None = None):
    """existing_docs: list of (title, doc_id) tuples that list_documents should return."""
    from pb2craft.api.craft import CraftDocument
    craft = AsyncMock()
    craft.ensure_folder.return_value = "folder-1"

    docs = [CraftDocument(id=doc_id, title=title) for title, doc_id in (existing_docs or [])]
    craft.list_documents.return_value = docs
    # Sync calls create_document + (optional upload_image) + insert_blocks
    craft.create_document.return_value = "doc-created"
    craft.upload_image.return_value = "block-cover"
    craft.insert_blocks.return_value = None
    return craft


# --------------------------------------------------------------------------- #
# First-time sync                                                              #
# --------------------------------------------------------------------------- #


async def test_first_time_sync_creates_doc_and_records_state(state: SyncStateStore):
    book = make_book(id="b1", title="My Book")
    h = make_highlight(id="h1", uuid="h1", book_id="b1", text="hello")

    pb = _mock_pocketbook([book], {"b1": [h]})
    craft = _mock_craft()  # empty folder

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    result = await svc.sync()

    craft.ensure_folder.assert_awaited_once_with("PocketBook Imports")
    craft.list_documents.assert_awaited_once()
    craft.create_document.assert_awaited_once()
    craft.insert_blocks.assert_awaited_once()
    assert result.total_books == 1
    assert result.total_highlights == 1
    assert state.get_book("b1") is not None
    assert state.get_synced_highlight_ids("b1") == {"h1"}
    assert state.get_folder_id() == "folder-1"


async def test_re_link_when_doc_already_exists_in_folder(state: SyncStateStore):
    """No state, but a doc with the matching title is in the Craft folder."""
    book = make_book(id="b1", title="My Book")
    h = make_highlight(id="h1", uuid="h1", book_id="b1")

    pb = _mock_pocketbook([book], {"b1": [h]})
    craft = _mock_craft(existing_docs=[("My Book", "doc-existing")])

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    result = await svc.sync()

    # Should re-link without re-uploading
    craft.create_document.assert_not_awaited()
    craft.insert_blocks.assert_not_awaited()
    craft.append_to_book_document.assert_not_awaited()

    synced = state.get_book("b1")
    assert synced is not None
    assert synced.craft_doc_id == "doc-existing"
    assert state.get_synced_highlight_ids("b1") == {"h1"}
    # Re-link doesn't count as "new pushes"
    assert result.total_books == 0
    assert result.total_highlights == 0


# --------------------------------------------------------------------------- #
# Incremental                                                                  #
# --------------------------------------------------------------------------- #


async def test_incremental_appends_only_new(state: SyncStateStore):
    book = make_book(id="b1", title="My Book")
    old = make_highlight(
        id="h1", uuid="h1", book_id="b1", text="first",
        begin="epubcfi(/6/14!/4/2/1:0)", end="epubcfi(/6/14!/4/2/1:5)",
        created=utc(2024, 1, 1, 10),
    )
    new = make_highlight(
        id="h2", uuid="h2", book_id="b1", text="second",
        begin="epubcfi(/6/14!/4/2/1:20)", end="epubcfi(/6/14!/4/2/1:25)",
        created=utc(2024, 1, 2, 10),
    )

    # Seed state: h1 already synced
    state.mark_book_synced(book_id="b1", craft_doc_id="doc-old", title="My Book", highlight_count=1)
    state.mark_highlights_synced(book_id="b1", highlight_uuids=["h1"])
    state.set_folder_id("folder-1")

    pb = _mock_pocketbook([book], {"b1": [old, new]})
    # Recorded doc must be present in the folder for incremental path to fire
    craft = _mock_craft(existing_docs=[("My Book", "doc-old")])

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    result = await svc.sync()

    craft.append_to_book_document.assert_awaited_once()
    append_kwargs = craft.append_to_book_document.await_args.kwargs
    assert append_kwargs["document_id"] == "doc-old"
    # Block payload should mention Highlight 2 (existing_count was 1)
    blocks = append_kwargs["blocks"]
    assert any("Highlight 2" in b["markdown"] for b in blocks)

    assert result.total_highlights == 1
    assert state.get_synced_highlight_ids("b1") == {"h1", "h2"}
    assert state.get_book("b1").highlight_count == 2  # type: ignore[union-attr]


async def test_incremental_with_no_new_highlights_skips(state: SyncStateStore):
    book = make_book(id="b1", title="My Book")
    h = make_highlight(id="h1", uuid="h1", book_id="b1")

    state.mark_book_synced(book_id="b1", craft_doc_id="doc-1", title="My Book", highlight_count=1)
    state.mark_highlights_synced(book_id="b1", highlight_uuids=["h1"])
    state.set_folder_id("folder-1")

    pb = _mock_pocketbook([book], {"b1": [h]})
    craft = _mock_craft(existing_docs=[("My Book", "doc-1")])

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    result = await svc.sync()

    craft.append_to_book_document.assert_not_awaited()
    assert result.total_highlights == 0
    assert result.skipped_highlights == 1


# --------------------------------------------------------------------------- #
# Force                                                                        #
# --------------------------------------------------------------------------- #


async def test_force_resets_state_then_relinks(state: SyncStateStore):
    book = make_book(id="b1", title="My Book")
    h = make_highlight(id="h1", uuid="h1", book_id="b1")

    state.mark_book_synced(book_id="b1", craft_doc_id="doc-stale", title="My Book", highlight_count=5)
    state.mark_highlights_synced(book_id="b1", highlight_uuids=["h1"])

    pb = _mock_pocketbook([book], {"b1": [h]})
    craft = _mock_craft(existing_docs=[("My Book", "doc-found")])

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync(force=True)

    # State was cleared then refreshed by matching against the folder listing
    synced = state.get_book("b1")
    assert synced is not None
    assert synced.craft_doc_id == "doc-found"


# --------------------------------------------------------------------------- #
# Edge cases                                                                   #
# --------------------------------------------------------------------------- #


async def test_books_without_highlights_are_skipped(state: SyncStateStore):
    book = make_book(id="b1", title="Empty Book")
    pb = _mock_pocketbook([book], {"b1": []})
    craft = _mock_craft()

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    result = await svc.sync()

    craft.create_book_document.assert_not_awaited()
    assert result.total_books == 0
    assert state.get_book("b1") is None


async def test_one_book_failing_does_not_stop_others(state: SyncStateStore):
    good_book = make_book(id="good", title="Good Book")
    bad_book = make_book(id="bad", title="Bad Book")
    h_good = make_highlight(id="hg", uuid="hg", book_id="good")
    h_bad = make_highlight(id="hb", uuid="hb", book_id="bad")

    pb = _mock_pocketbook([bad_book, good_book], {"good": [h_good], "bad": [h_bad]})

    craft = _mock_craft()
    # Make the bad book fail on the create call
    async def create(title: str, *, folder_id: str):
        if title == "Bad Book":
            raise CraftError("simulated")
        return "doc-good"
    craft.create_document.side_effect = create

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    result = await svc.sync()

    assert len(result.errors) == 1
    assert "Bad Book" in result.errors[0]
    assert state.get_book("good") is not None  # good book still synced
    assert state.get_book("bad") is None


async def test_existing_docs_cache_avoids_n_plus_one(state: SyncStateStore):
    """Many first-time books should trigger list_documents exactly once."""
    books = [make_book(id=f"b{i}", title=f"Book {i}") for i in range(5)]
    highlights = {b.id: [make_highlight(id=f"h{b.id}", uuid=f"h{b.id}", book_id=b.id)]
                  for b in books}

    pb = _mock_pocketbook(books, highlights)
    craft = _mock_craft()  # empty folder; every book is a fresh create

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync()

    # One list_documents for the whole run, regardless of book count
    assert craft.list_documents.await_count == 1
    assert craft.create_document.await_count == 5
    assert craft.insert_blocks.await_count == 5


async def test_existing_docs_cache_reloads_each_sync(state: SyncStateStore):
    """The cache resets between sync runs so changes in Craft are reflected."""
    from pb2craft.api.craft import CraftDocument

    book = make_book(id="b1", title="My Book")
    h = make_highlight(id="h1", uuid="h1", book_id="b1")

    pb = _mock_pocketbook([book], {"b1": [h]})
    craft = _mock_craft()  # first run: empty folder, will create

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync()

    # Second run: book is in state, doc still in folder — incremental path
    # verifies the doc still exists, so list_documents fires again.
    craft.list_documents.return_value = [CraftDocument(id="doc-created", title="My Book")]
    await svc.sync()

    assert craft.list_documents.await_count == 2


async def test_recreates_doc_when_recorded_one_was_deleted(state: SyncStateStore):
    """User deleted the Craft doc but state still thinks it's there — recreate it."""
    book = make_book(id="b1", title="My Book")
    h = make_highlight(id="h1", uuid="h1", book_id="b1")

    # Seed state pointing at a doc that no longer exists in Craft
    state.mark_book_synced(book_id="b1", craft_doc_id="doc-deleted", title="My Book", highlight_count=1)
    state.mark_highlights_synced(book_id="b1", highlight_uuids=["h1"])
    state.set_folder_id("folder-1")

    pb = _mock_pocketbook([book], {"b1": [h]})
    craft = _mock_craft()  # folder is empty — doc-deleted isn't there

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync()

    # Falls back to first-time sync: doc is gone AND not findable by title, so create fresh
    craft.create_document.assert_awaited_once()
    craft.insert_blocks.assert_awaited_once()

    synced = state.get_book("b1")
    assert synced is not None
    assert synced.craft_doc_id == "doc-created"  # the new id from the mock


async def test_relinks_when_recorded_doc_replaced_with_same_title(state: SyncStateStore):
    """User deleted the doc and Craft has another doc with the same title — re-link to it."""
    from pb2craft.api.craft import CraftDocument

    book = make_book(id="b1", title="My Book")
    h = make_highlight(id="h1", uuid="h1", book_id="b1")

    state.mark_book_synced(book_id="b1", craft_doc_id="doc-stale", title="My Book", highlight_count=1)
    state.mark_highlights_synced(book_id="b1", highlight_uuids=["h1"])
    state.set_folder_id("folder-1")

    pb = _mock_pocketbook([book], {"b1": [h]})
    craft = _mock_craft()
    # Folder now contains a different doc with the same title (e.g. user recreated it manually)
    craft.list_documents.return_value = [CraftDocument(id="doc-new", title="My Book")]

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync()

    # No re-upload — title match found, just re-link the state
    craft.create_document.assert_not_awaited()
    craft.insert_blocks.assert_not_awaited()
    assert state.get_book("b1").craft_doc_id == "doc-new"  # type: ignore[union-attr]


async def test_pocketbook_error_in_book_is_isolated(state: SyncStateStore):
    book = make_book(id="b1", title="My Book")
    pb = AsyncMock()
    pb.get_books.return_value = [book]
    pb.get_highlights.side_effect = PocketBookError("API down")

    craft = _mock_craft()
    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    result = await svc.sync()

    assert len(result.errors) == 1
    assert "My Book" in result.errors[0]
