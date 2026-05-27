"""Unit tests for CraftClient — mocks HTTP via respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from pb2craft.api.craft import (
    CraftAuthError,
    CraftClient,
    CraftError,
    CraftRateLimited,
)

API_URL = "https://connect.craft.do/links/TEST/api/v1"
TOKEN = "test-token"  # noqa: S105 — fake test token


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def client():
    c = CraftClient(api_url=API_URL, token=TOKEN)
    # Bypass throttle in tests — they don't need real delays
    c.MIN_REQUEST_INTERVAL = 0
    yield c
    await c.aclose()


def _auth_header_seen(route: respx.Route) -> bool:
    """Did the mocked route receive the Bearer header?"""
    return any(
        call.request.headers.get("Authorization") == f"Bearer {TOKEN}"
        for call in route.calls
    )


# --------------------------------------------------------------------------- #
# Folders                                                                      #
# --------------------------------------------------------------------------- #


@respx.mock
async def test_ensure_folder_returns_existing(client: CraftClient):
    route = respx.get(f"{API_URL}/folders").mock(
        return_value=httpx.Response(
            200,
            json={"folders": [{"id": "f-existing", "name": "PocketBook Imports"}]},
        )
    )

    folder_id = await client.ensure_folder("PocketBook Imports")

    assert folder_id == "f-existing"
    assert _auth_header_seen(route)


@respx.mock
async def test_ensure_folder_creates_when_missing(client: CraftClient):
    respx.get(f"{API_URL}/folders").mock(
        return_value=httpx.Response(200, json={"folders": []})
    )
    create_route = respx.post(f"{API_URL}/folders").mock(
        return_value=httpx.Response(
            200,
            json={"folders": [{"id": "f-new", "name": "PocketBook Imports"}]},
        )
    )

    folder_id = await client.ensure_folder("PocketBook Imports")

    assert folder_id == "f-new"
    payload = create_route.calls.last.request.read()
    assert b'"name": "PocketBook Imports"' in payload or b'"name":"PocketBook Imports"' in payload


# --------------------------------------------------------------------------- #
# Documents                                                                    #
# --------------------------------------------------------------------------- #


@respx.mock
async def test_create_book_document_chains_two_calls(client: CraftClient):
    docs_route = respx.post(f"{API_URL}/documents").mock(
        return_value=httpx.Response(
            200,
            json={"documents": [{"id": "doc-1", "title": "My Book"}]},
        )
    )
    blocks_route = respx.post(f"{API_URL}/blocks").mock(
        return_value=httpx.Response(200, json={})
    )

    doc_id = await client.create_book_document(
        title="My Book",
        blocks=[{"type": "text", "markdown": "## Hello"}],
        folder_id="f-1",
    )

    assert doc_id == "doc-1"
    # POST /documents body has title + folderId destination
    docs_body = docs_route.calls.last.request.read()
    assert b'"title": "My Book"' in docs_body or b'"title":"My Book"' in docs_body
    assert b'f-1' in docs_body
    # POST /blocks body references the new doc as pageId at end
    blocks_body = blocks_route.calls.last.request.read()
    assert b"doc-1" in blocks_body
    assert b"end" in blocks_body


@respx.mock
async def test_append_to_book_document_uses_existing_doc_id(client: CraftClient):
    blocks_route = respx.post(f"{API_URL}/blocks").mock(
        return_value=httpx.Response(200, json={})
    )

    await client.append_to_book_document(
        document_id="doc-existing",
        blocks=[{"type": "text", "markdown": "### Highlight 6"}],
    )

    body = blocks_route.calls.last.request.read()
    assert b"doc-existing" in body
    assert b"Highlight 6" in body


@respx.mock
async def test_append_to_book_document_skips_empty_blocks(client: CraftClient):
    route = respx.post(f"{API_URL}/blocks").mock(
        return_value=httpx.Response(200, json={})
    )

    await client.append_to_book_document(document_id="doc-1", blocks=[])

    assert not route.called


@respx.mock
async def test_find_book_document_by_title(client: CraftClient):
    respx.get(f"{API_URL}/documents").mock(
        return_value=httpx.Response(
            200,
            json={
                "documents": [
                    {"id": "doc-a", "title": "Another Book"},
                    {"id": "doc-b", "title": "The Book I Want"},
                ]
            },
        )
    )

    doc_id = await client.find_book_document("The Book I Want", folder_id="f-1")

    assert doc_id == "doc-b"


@respx.mock
async def test_find_book_document_returns_none_when_absent(client: CraftClient):
    respx.get(f"{API_URL}/documents").mock(
        return_value=httpx.Response(200, json={"documents": []})
    )

    doc_id = await client.find_book_document("Missing", folder_id="f-1")

    assert doc_id is None


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #


@respx.mock
async def test_401_raises_auth_error(client: CraftClient):
    respx.get(f"{API_URL}/folders").mock(return_value=httpx.Response(401))

    with pytest.raises(CraftAuthError):
        await client.list_folders()


@respx.mock
async def test_429_raises_rate_limited(client: CraftClient):
    respx.post(f"{API_URL}/blocks").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"})
    )

    with pytest.raises(CraftRateLimited):
        await client.insert_blocks(
            document_id="doc-1",
            blocks=[{"type": "text", "markdown": "x"}],
        )


@respx.mock
async def test_create_document_empty_response_raises(client: CraftClient):
    respx.post(f"{API_URL}/documents").mock(
        return_value=httpx.Response(200, json={"documents": []})
    )

    with pytest.raises(CraftError):
        await client.create_document("Some Title", folder_id="f-1")
