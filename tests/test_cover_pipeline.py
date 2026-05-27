"""End-to-end-ish tests for the cover upload pipeline in sync.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from pb2craft.api.craft import CraftClient
from pb2craft.api.pocketbook import PocketBookClient
from pb2craft.models import Book
from pb2craft.state import SyncStateStore
from pb2craft.sync import SyncService

from tests.conftest import make_highlight


PB_BASE = PocketBookClient.BASE_URL
CRAFT_URL = "https://connect.craft.do/links/T/api/v1"


def _book_with_cover() -> Book:
    return Book.model_validate({
        "id": "b1",
        "fast_hash": "h1",
        "title": "Cover Book",
        "metadata": {
            "authors": "An Author",
            "publisher": "Pub",
            "isbn": "9780000000000",
            "lang": "en",
            "cover": [
                {"width": 520, "height": 800, "path": "https://example/cover_b.jpg"},
            ],
        },
    })


def _book_without_cover() -> Book:
    return Book.model_validate({"id": "b2", "fast_hash": "h2", "title": "No Cover Book"})


@pytest.fixture
def state(tmp_path):
    return SyncStateStore(tmp_path / "state.sqlite")


# --------------------------------------------------------------------------- #
# PocketBookClient.download_cover                                              #
# --------------------------------------------------------------------------- #


@respx.mock
async def test_download_cover_returns_bytes():
    respx.get("https://example/cover.jpg").mock(
        return_value=httpx.Response(200, content=b"JPEGDATA")
    )

    from pb2craft.api.pocketbook import InMemoryTokenStore
    pb = PocketBookClient(token_store=InMemoryTokenStore())
    try:
        data = await pb.download_cover("https://example/cover.jpg")
        assert data == b"JPEGDATA"
    finally:
        await pb.aclose()


# --------------------------------------------------------------------------- #
# CraftClient.upload_image                                                     #
# --------------------------------------------------------------------------- #


@respx.mock
async def test_upload_image_posts_binary_body():
    route = respx.post(f"{CRAFT_URL}/upload").mock(
        return_value=httpx.Response(200, json={"blockId": "block-xyz", "assetUrl": "x"})
    )

    client = CraftClient(api_url=CRAFT_URL, token="t")
    client.MIN_REQUEST_INTERVAL = 0
    try:
        block_id = await client.upload_image(
            document_id="doc-1",
            image_bytes=b"PNGDATA",
            content_type="image/png",
        )
        assert block_id == "block-xyz"

        request = route.calls.last.request
        assert request.url.params["pageId"] == "doc-1"
        assert request.url.params["position"] == "start"
        assert request.headers["Authorization"] == "Bearer t"
        assert request.headers["Content-Type"] == "image/png"
        assert request.read() == b"PNGDATA"
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# SyncService cover orchestration                                              #
# --------------------------------------------------------------------------- #


def _mock_pb_with_book(book: Book, highlight) -> AsyncMock:
    pb = AsyncMock()
    pb.get_books.return_value = [book]
    pb.get_highlights.return_value = [highlight]
    pb.download_cover.return_value = b"JPEGBYTES"
    return pb


def _mock_craft_empty() -> AsyncMock:
    craft = AsyncMock()
    craft.ensure_folder.return_value = "folder-1"
    craft.list_documents.return_value = []
    craft.create_document.return_value = "doc-1"
    craft.upload_image.return_value = "block-cover"
    craft.insert_blocks.return_value = None
    return craft


async def test_sync_uploads_cover_for_new_book(state: SyncStateStore):
    book = _book_with_cover()
    h = make_highlight(id="h1", uuid="h1", book_id="b1")

    pb = _mock_pb_with_book(book, h)
    craft = _mock_craft_empty()

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync()

    pb.download_cover.assert_awaited_once_with("https://example/cover_b.jpg")
    craft.upload_image.assert_awaited_once()
    upload_kwargs = craft.upload_image.await_args.kwargs
    assert upload_kwargs["document_id"] == "doc-1"
    assert upload_kwargs["image_bytes"] == b"JPEGBYTES"
    assert upload_kwargs["position"] == "start"
    # Text blocks still inserted after
    craft.insert_blocks.assert_awaited_once()


async def test_sync_skips_cover_when_book_has_none(state: SyncStateStore):
    book = _book_without_cover()
    h = make_highlight(id="h1", uuid="h1", book_id="b2")

    pb = _mock_pb_with_book(book, h)
    craft = _mock_craft_empty()

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync()

    pb.download_cover.assert_not_awaited()
    craft.upload_image.assert_not_awaited()
    # Text blocks still inserted
    craft.insert_blocks.assert_awaited_once()


async def test_sync_tolerates_cover_download_failure(state: SyncStateStore):
    book = _book_with_cover()
    h = make_highlight(id="h1", uuid="h1", book_id="b1")

    pb = _mock_pb_with_book(book, h)
    pb.download_cover.side_effect = RuntimeError("network gone")
    craft = _mock_craft_empty()

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    result = await svc.sync()

    # Book still synced, just without a cover
    assert result.total_books == 1
    craft.upload_image.assert_not_awaited()
    craft.insert_blocks.assert_awaited_once()


async def test_sync_tolerates_cover_upload_failure(state: SyncStateStore):
    book = _book_with_cover()
    h = make_highlight(id="h1", uuid="h1", book_id="b1")

    pb = _mock_pb_with_book(book, h)
    craft = _mock_craft_empty()
    # Two persistent failures → retry exhausted, give up gracefully
    craft.upload_image.side_effect = RuntimeError("Craft upload failed")

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    result = await svc.sync()

    assert result.total_books == 1
    # We retry once, so two upload attempts total
    assert craft.upload_image.await_count == 2
    # Text blocks still inserted despite cover upload failure
    craft.insert_blocks.assert_awaited_once()


async def test_sync_retries_cover_upload_on_transient_error(state: SyncStateStore, monkeypatch):
    """Transient SSL/network error on first attempt → retry with fresh connection."""
    book = _book_with_cover()
    h = make_highlight(id="h1", uuid="h1", book_id="b1")

    pb = _mock_pb_with_book(book, h)
    craft = _mock_craft_empty()
    # First call fails, second succeeds
    craft.upload_image.side_effect = [
        RuntimeError("[SSL: SSLV3_ALERT_BAD_RECORD_MAC] ssl/tls alert bad record mac"),
        "block-id-ok",
    ]

    # Skip the 1-second sleep in tests
    async def _no_sleep(_seconds):
        pass
    monkeypatch.setattr("pb2craft.sync.asyncio.sleep", _no_sleep)

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    result = await svc.sync()

    assert result.total_books == 1
    assert craft.upload_image.await_count == 2  # first failed, retry succeeded
    # Retry must drop the bad pooled connection
    craft.reset_connections.assert_awaited_once()
    craft.insert_blocks.assert_awaited_once()


async def test_relink_does_not_upload_cover(state: SyncStateStore):
    """When a doc already exists in the folder, we re-link without touching content."""
    from pb2craft.api.craft import CraftDocument

    book = _book_with_cover()
    h = make_highlight(id="h1", uuid="h1", book_id="b1")

    pb = _mock_pb_with_book(book, h)
    craft = _mock_craft_empty()
    craft.list_documents.return_value = [CraftDocument(id="doc-existing", title="Cover Book")]

    svc = SyncService(pocketbook=pb, craft=craft, state=state)
    await svc.sync()

    pb.download_cover.assert_not_awaited()
    craft.upload_image.assert_not_awaited()
    craft.create_document.assert_not_awaited()
