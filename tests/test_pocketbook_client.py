"""Unit tests for PocketBookClient — mocks HTTP via respx."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from pb2craft.api.pocketbook import (
    InMemoryTokenStore,
    PocketBookAuthError,
    PocketBookClient,
    PocketBookCredentials,
    PocketBookRateLimited,
    Shop,
)

BASE = PocketBookClient.BASE_URL


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def client():
    """Client with an empty in-memory token store and a real httpx instance."""
    store = InMemoryTokenStore()
    c = PocketBookClient(token_store=store)
    yield c
    await c.aclose()


def _seed_valid_creds(store: InMemoryTokenStore, *, expired: bool = False) -> None:
    now = datetime.now(tz=timezone.utc)
    expires_at = now - timedelta(minutes=5) if expired else now + timedelta(hours=1)
    store.save(
        PocketBookCredentials(
            access_token="access-1",
            refresh_token="refresh-1",
            expires_at=expires_at,
            shop_alias="us",
        )
    )


# --------------------------------------------------------------------------- #
# Auth                                                                         #
# --------------------------------------------------------------------------- #


@respx.mock
async def test_get_shops_returns_providers(client: PocketBookClient):
    respx.get(f"{BASE}/auth/login").mock(
        return_value=httpx.Response(
            200,
            json={"providers": [{"alias": "us", "name": "US Shop", "shop_id": "1"}]},
        )
    )

    shops = await client.get_shops("user@example.com")

    assert len(shops) == 1
    assert shops[0].alias == "us"
    assert shops[0].shop_id == "1"


@respx.mock
async def test_login_stores_credentials(client: PocketBookClient):
    respx.post(f"{BASE}/auth/login/us").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "ACCESS",
                "refresh_token": "REFRESH",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )
    )

    creds = await client.login(
        email="user@example.com",
        password="pw",
        shop=Shop(alias="us", name="US", shop_id="1"),
    )

    assert creds.access_token == "ACCESS"
    assert creds.shop_alias == "us"
    stored = client.token_store.load()
    assert stored is not None
    assert stored.refresh_token == "REFRESH"
    assert stored.is_valid


@respx.mock
async def test_expired_token_triggers_refresh(client: PocketBookClient):
    _seed_valid_creds(client.token_store, expired=True)

    respx.post(f"{BASE}/auth/renew-token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "ACCESS-NEW",
                "refresh_token": "REFRESH-NEW",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )
    )
    respx.get(f"{BASE}/books").mock(
        return_value=httpx.Response(200, json={"total": 0, "items": []})
    )

    await client.get_books()

    stored = client.token_store.load()
    assert stored is not None
    assert stored.access_token == "ACCESS-NEW"


async def test_ensure_token_raises_when_not_authenticated(client: PocketBookClient):
    with pytest.raises(PocketBookAuthError):
        await client.get_books()


# --------------------------------------------------------------------------- #
# Books                                                                        #
# --------------------------------------------------------------------------- #


@respx.mock
async def test_get_books_returns_list(client: PocketBookClient):
    _seed_valid_creds(client.token_store)

    respx.get(f"{BASE}/books").mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 2,
                "items": [
                    {"id": "b1", "fast_hash": "h1", "title": "Book One", "path": "/Author - One.epub"},
                    {"id": "b2", "fast_hash": "h2", "title": "Book Two"},
                ],
            },
        )
    )

    books = await client.get_books()

    assert [b.id for b in books] == ["b1", "b2"]
    assert books[0].title == "Book One"


# --------------------------------------------------------------------------- #
# Highlights                                                                   #
# --------------------------------------------------------------------------- #


@respx.mock
async def test_get_highlights_filters_bookmark_markers(client: PocketBookClient):
    _seed_valid_creds(client.token_store)
    from pb2craft.models import Book

    book = Book(id="b1", fast_hash="h1", title="Book")

    # The IDs endpoint returns an array directly (not wrapped in {"data": ...})
    respx.get(f"{BASE}/notes").mock(
        return_value=httpx.Response(
            200, json=[{"uuid": "n1"}, {"uuid": "n2"}, {"uuid": "n3"}]
        )
    )

    # n1: real highlight, n2: bookmark marker (filtered), n3: 404 (skipped)
    respx.get(f"{BASE}/notes/n1").mock(
        return_value=httpx.Response(
            200,
            json={
                "uuid": "n1",
                "color": {"value": "yellow"},
                "quotation": {
                    "begin": "epubcfi(/6/14!/4/2/1:0)",
                    "end": "epubcfi(/6/14!/4/2/1:10)",
                    "text": "real highlighted text",
                },
                "mark": {"anchor": "pbr:/page?page=12", "created": 1704110400.0},
            },
        )
    )
    respx.get(f"{BASE}/notes/n2").mock(
        return_value=httpx.Response(
            200,
            json={
                "uuid": "n2",
                "quotation": {"begin": "", "end": "", "text": "Bookmark"},
            },
        )
    )
    respx.get(f"{BASE}/notes/n3").mock(return_value=httpx.Response(404))

    highlights = await client.get_highlights(book)

    assert len(highlights) == 1
    assert highlights[0].text == "real highlighted text"
    assert highlights[0].color.value == "yellow"
    assert highlights[0].mark is not None
    assert highlights[0].mark.page == 12


@respx.mock
async def test_get_highlight_handles_iso_timestamp(client: PocketBookClient):
    _seed_valid_creds(client.token_store)

    respx.get(f"{BASE}/notes/n1").mock(
        return_value=httpx.Response(
            200,
            json={
                "uuid": "n1",
                "color": {"value": "red"},
                "quotation": {
                    "begin": "epubcfi(/6/14!/4/2/1:0)",
                    "end": "epubcfi(/6/14!/4/2/1:10)",
                    "text": "iso timestamp test",
                    "updated": "2024-06-15T10:30:00.000Z",
                },
                "mark": {"anchor": "pbr:/page?page=1", "created": "2024-06-15T10:30:00.000Z"},
            },
        )
    )

    raw = await client.get_highlight("n1", fast_hash="h1")
    assert raw is not None
    h = raw.to_highlight(book_id="b1", book_fast_hash="h1")
    assert h is not None
    assert h.quotation.updated is not None
    assert h.mark is not None
    assert h.mark.created is not None
    assert h.mark.created.year == 2024


# --------------------------------------------------------------------------- #
# Error mapping                                                                #
# --------------------------------------------------------------------------- #


@respx.mock
async def test_401_raises_auth_error(client: PocketBookClient):
    _seed_valid_creds(client.token_store)

    respx.get(f"{BASE}/books").mock(return_value=httpx.Response(401))

    with pytest.raises(PocketBookAuthError):
        await client.get_books()


@respx.mock
async def test_429_raises_rate_limited(client: PocketBookClient):
    _seed_valid_creds(client.token_store)

    respx.get(f"{BASE}/books").mock(return_value=httpx.Response(429))

    with pytest.raises(PocketBookRateLimited):
        await client.get_books()
