"""PocketBook Cloud API client.

Port of Sources/PocketBook2CapacitiesCore/API/PocketBookClient.swift.

Auth flow:
  1. ``get_shops(username)`` → discover the user's shop (provider/region)
  2. ``login(email, password, shop)`` → obtain access + refresh tokens
  3. Tokens are persisted via :class:`TokenStore`; later calls auto-refresh.

Token refresh is guarded by an asyncio lock so concurrent requests do not each
trigger a refresh (mirrors the Swift actor's serialized access).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Protocol

import httpx
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from pb2craft.models import (
    Book,
    Highlight,
    HighlightColor,
    Mark,
    Quotation,
    is_bookmark_marker,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Credentials + token store                                                    #
# --------------------------------------------------------------------------- #


class PocketBookCredentials(BaseModel):
    """Persisted PocketBook auth state."""

    access_token: str
    refresh_token: str
    expires_at: datetime
    shop_alias: str

    @property
    def is_valid(self) -> bool:
        """True when the access token hasn't expired (with a 60s buffer)."""
        return datetime.now(tz=timezone.utc) < self.expires_at - timedelta(seconds=60)


class TokenStore(Protocol):
    """Minimal load/save protocol so storage can be swapped in Phase 5."""

    def load(self) -> PocketBookCredentials | None: ...
    def save(self, creds: PocketBookCredentials) -> None: ...
    def clear(self) -> None: ...


class InMemoryTokenStore:
    """Test-friendly TokenStore. Phase 5 adds a file-backed implementation."""

    def __init__(self, creds: PocketBookCredentials | None = None) -> None:
        self._creds = creds

    def load(self) -> PocketBookCredentials | None:
        return self._creds

    def save(self, creds: PocketBookCredentials) -> None:
        self._creds = creds

    def clear(self) -> None:
        self._creds = None


# --------------------------------------------------------------------------- #
# Wire-format models                                                           #
# --------------------------------------------------------------------------- #


class Shop(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    alias: str
    name: str
    shop_id: str | None = Field(default=None, alias="shop_id")


class _ShopsResponse(BaseModel):
    providers: list[Shop]


class _AuthTokens(BaseModel):
    """Raw response of /auth/login and /auth/renew-token."""

    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str | None = None


class _BooksResponse(BaseModel):
    total: int
    items: list[Book]


class _HighlightIdEntry(BaseModel):
    uuid: str


# ----- raw /notes/{uuid} response with flexible timestamp parsing ---------- #


def _parse_flexible_timestamp(value: object) -> datetime | None:
    """Accept ISO-8601 string OR unix number OR None."""
    if value is None or isinstance(value, datetime):
        return value  # type: ignore[return-value]
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


FlexibleTimestamp = Annotated[datetime | None, BeforeValidator(_parse_flexible_timestamp)]


class _NoteTypeValue(BaseModel):
    value: str | None = None


class _NoteColorType(BaseModel):
    value: str | None = None


class _NoteContent(BaseModel):
    text: str | None = None


class _NoteQuotationType(BaseModel):
    begin: str | None = None
    end: str | None = None
    text: str | None = None
    updated: str | None = None


class _NoteMarkType(BaseModel):
    anchor: str | None = None
    created: FlexibleTimestamp = None
    updated: FlexibleTimestamp = None


class _NoteResponse(BaseModel):
    """Raw /notes/{uuid} response shape."""

    uuid: str
    color: _NoteColorType | None = None
    type: _NoteTypeValue | None = None
    note: _NoteContent | None = None
    quotation: _NoteQuotationType | None = None
    mark: _NoteMarkType | None = None

    def to_highlight(self, *, book_id: str, book_fast_hash: str) -> Highlight | None:
        if not self.quotation or not self.quotation.text:
            return None

        text = self.quotation.text
        if is_bookmark_marker(text):
            return None

        quotation_updated = _parse_iso(self.quotation.updated)

        return Highlight(
            id=self.uuid,
            uuid=self.uuid,
            book_id=book_id,
            book_fast_hash=book_fast_hash,
            color=HighlightColor(value=(self.color.value if self.color else None) or "unknown"),
            note=self.note.text if self.note else None,
            text=text,
            quotation=Quotation(
                begin=self.quotation.begin or "",
                end=self.quotation.end or "",
                text=text,
                updated=quotation_updated,
            ),
            mark=Mark(
                anchor=self.mark.anchor if self.mark else None,
                created=self.mark.created if self.mark else None,
                updated=self.mark.updated if self.mark else None,
            ),
        )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #


class PocketBookError(Exception):
    """Base for PocketBook API errors."""


class PocketBookAuthError(PocketBookError):
    """Token missing / invalid / refresh failed."""


class PocketBookNotFound(PocketBookError):
    pass


class PocketBookRateLimited(PocketBookError):
    pass


class PocketBookHttpError(PocketBookError):
    def __init__(self, status_code: int, body: str = ""):
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}: {body[:200]}")


# --------------------------------------------------------------------------- #
# Client                                                                       #
# --------------------------------------------------------------------------- #


class PocketBookClient:
    """Async PocketBook Cloud client.

    Pass in an :class:`httpx.AsyncClient` to share connections / control
    timeouts; otherwise a default one is created and closed via ``aclose()``.
    """

    BASE_URL = "https://cloud.pocketbook.digital/api/v1.0"
    # These are public constants embedded in the PocketBook Cloud frontend
    # and the predecessor Mac app — not secret credentials.
    CLIENT_ID = "qNAx1RDb"
    CLIENT_SECRET = "K3YYSjCgDJNoWKdGVOyO1mrROp3MMZqqRNXNXTmh"

    def __init__(
        self,
        token_store: TokenStore,
        http: httpx.AsyncClient | None = None,
    ):
        self.token_store = token_store
        self._http = http
        self._owns_http = http is None
        self._refresh_lock = asyncio.Lock()

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=120.0))
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # ---------------------------------------------------------------------- #
    # Authentication                                                          #
    # ---------------------------------------------------------------------- #

    async def get_shops(self, username: str) -> list[Shop]:
        response = await self.http.get(
            f"{self.BASE_URL}/auth/login",
            params={
                "username": username,
                "client_id": self.CLIENT_ID,
                "client_secret": self.CLIENT_SECRET,
            },
            headers={"Cache-Control": "no-cache"},
        )
        _check(response)
        return _ShopsResponse.model_validate_json(response.content).providers

    async def login(self, email: str, password: str, shop: Shop) -> PocketBookCredentials:
        shop_id = shop.shop_id or "1"
        response = await self.http.post(
            f"{self.BASE_URL}/auth/login/{shop.alias}",
            data={
                "shop_id": shop_id,
                "username": email,
                "password": password,
                "client_id": self.CLIENT_ID,
                "client_secret": self.CLIENT_SECRET,
                "grant_type": "password",
                "language": "en",
            },
        )
        _check(response)
        tokens = _AuthTokens.model_validate_json(response.content)
        creds = _tokens_to_credentials(tokens, shop.alias)
        self.token_store.save(creds)
        return creds

    async def _refresh(self) -> None:
        """Refresh the access token. Caller must hold ``_refresh_lock``."""
        current = self.token_store.load()
        if current is None:
            raise PocketBookAuthError("Not authenticated")

        response = await self.http.post(
            f"{self.BASE_URL}/auth/renew-token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": current.refresh_token,
            },
            headers={"Authorization": f"Bearer {current.access_token}"},
        )
        _check(response)
        tokens = _AuthTokens.model_validate_json(response.content)
        self.token_store.save(_tokens_to_credentials(tokens, current.shop_alias))

    async def _ensure_valid_token(self) -> str:
        async with self._refresh_lock:
            current = self.token_store.load()
            if current is None:
                raise PocketBookAuthError("Not authenticated")
            if not current.is_valid:
                await self._refresh()
                current = self.token_store.load()
                if current is None:
                    raise PocketBookAuthError("Token refresh failed")
            return current.access_token

    # ---------------------------------------------------------------------- #
    # Books                                                                   #
    # ---------------------------------------------------------------------- #

    async def get_books(self, *, limit: int = 500) -> list[Book]:
        token = await self._ensure_valid_token()
        response = await self.http.get(
            f"{self.BASE_URL}/books",
            params={"limit": limit},
            headers={
                "Authorization": f"Bearer {token}",
                "Cache-Control": "no-cache",
            },
        )
        _check(response)
        return _BooksResponse.model_validate_json(response.content).items

    # ---------------------------------------------------------------------- #
    # Highlights                                                              #
    # ---------------------------------------------------------------------- #

    async def get_highlight_ids(self, book: Book) -> list[str]:
        token = await self._ensure_valid_token()
        response = await self.http.get(
            f"{self.BASE_URL}/notes",
            params={"fast_hash": book.fast_hash},
            headers={
                "Authorization": f"Bearer {token}",
                "Cache-Control": "no-cache",
            },
        )
        _check(response)
        entries = [_HighlightIdEntry.model_validate(item) for item in response.json()]
        return [e.uuid for e in entries]

    async def get_highlight(self, uuid: str, *, fast_hash: str) -> _NoteResponse | None:
        token = await self._ensure_valid_token()
        response = await self.http.get(
            f"{self.BASE_URL}/notes/{uuid}",
            params={"fast_hash": fast_hash},
            headers={
                "Authorization": f"Bearer {token}",
                "Cache-Control": "no-cache",
            },
        )
        if response.status_code == 404:
            return None
        _check(response)
        return _NoteResponse.model_validate_json(response.content)

    # ---------------------------------------------------------------------- #
    # Cover images                                                            #
    # ---------------------------------------------------------------------- #

    async def download_cover(self, url: str) -> bytes:
        """Fetch a book cover image. URL already carries the access token.

        PocketBook redirects cover requests to a signed CDN URL, so follow
        redirects.
        """
        response = await self.http.get(url, follow_redirects=True)
        _check(response)
        return response.content

    async def get_highlights(self, book: Book) -> list[Highlight]:
        ids = await self.get_highlight_ids(book)
        results: list[Highlight] = []
        for uuid in ids:
            raw = await self.get_highlight(uuid, fast_hash=book.fast_hash)
            if raw is None:
                continue
            highlight = raw.to_highlight(book_id=book.id, book_fast_hash=book.fast_hash)
            if highlight is not None:
                results.append(highlight)
        return results


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _tokens_to_credentials(tokens: _AuthTokens, shop_alias: str) -> PocketBookCredentials:
    return PocketBookCredentials(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_at=datetime.now(tz=timezone.utc) + timedelta(seconds=tokens.expires_in),
        shop_alias=shop_alias,
    )


def _check(response: httpx.Response) -> None:
    code = response.status_code
    if 200 <= code < 300:
        return
    body = response.text
    if code == 401:
        raise PocketBookAuthError("Unauthorized — credentials may have expired")
    if code == 403:
        raise PocketBookAuthError(f"Forbidden: {body[:200]}")
    if code == 404:
        raise PocketBookNotFound("Resource not found")
    if code == 429:
        raise PocketBookRateLimited("Rate limited — try again later")
    raise PocketBookHttpError(code, body)
