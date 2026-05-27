"""Craft.do API client.

Each Craft user creates an "All Documents" API connection in the Imagine tab,
yielding a unique base URL like::

    https://connect.craft.do/links/<id>/api/v1

Auth is ``Authorization: Bearer <token>``.

Endpoints used:
  POST /folders     → create our "PocketBook Imports" folder once
  GET  /folders     → discover existing folder
  GET  /documents   → list existing book docs (state recovery)
  POST /documents   → create empty doc shell ``{documents:[{title}], destination:{folderId}}``
  POST /blocks      → insert content ``{blocks:[{type:'text', markdown}], position:{pageId, position:'end'}}``
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from pydantic import BaseModel

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Wire models                                                                  #
# --------------------------------------------------------------------------- #


class CraftFolder(BaseModel):
    id: str
    name: str


class CraftDocument(BaseModel):
    id: str
    title: str


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #


class CraftError(Exception):
    """Base class for Craft API failures."""


class CraftAuthError(CraftError):
    pass


class CraftNotFound(CraftError):
    pass


class CraftRateLimited(CraftError):
    pass


class CraftHttpError(CraftError):
    def __init__(self, status_code: int, body: str = ""):
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}: {body[:200]}")


# --------------------------------------------------------------------------- #
# Client                                                                       #
# --------------------------------------------------------------------------- #


class CraftClient:
    """Async client for one Craft API connection."""

    MIN_REQUEST_INTERVAL = 0.5  # seconds — defensive throttle; Craft docs don't publish limits

    def __init__(
        self,
        api_url: str,
        token: str,
        http: httpx.AsyncClient | None = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self._http = http
        self._owns_http = http is None
        self._throttle_lock = asyncio.Lock()
        self._last_request_at = 0.0

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=60.0))
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def reset_connections(self) -> None:
        """Tear down the pooled HTTP session so the next request gets a fresh
        TLS connection. Used as recovery between retries when the server (or
        a flaky proxy) leaves the cached connection in a bad state — e.g.
        ``SSLV3_ALERT_BAD_RECORD_MAC`` on Craft's ``/upload`` endpoint.
        """
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # ---------------------------------------------------------------------- #
    # Folders                                                                 #
    # ---------------------------------------------------------------------- #

    async def list_folders(self) -> list[CraftFolder]:
        data = await self._request("GET", "/folders")
        items = _extract_items(data, "folders") or []
        return [CraftFolder.model_validate(item) for item in items]

    async def create_folder(self, name: str) -> CraftFolder:
        data = await self._request(
            "POST",
            "/folders",
            json={"folders": [{"name": name}]},
        )
        items = _extract_items(data, "folders")
        if not items:
            raise CraftError(
                f"create_folder returned no recognizable items for name={name!r}. "
                f"Raw response: {data!r}"
            )
        return CraftFolder.model_validate(items[0])

    async def ensure_folder(self, name: str) -> str:
        """Return the id of the folder named ``name``, creating it if missing."""
        try:
            for folder in await self.list_folders():
                if folder.name == name:
                    return folder.id
        except CraftHttpError as e:
            log.warning("list_folders failed (%s); attempting create anyway", e)
        return (await self.create_folder(name)).id

    # ---------------------------------------------------------------------- #
    # Documents                                                               #
    # ---------------------------------------------------------------------- #

    async def list_documents(self, *, folder_id: str | None = None) -> list[CraftDocument]:
        params: dict[str, Any] = {}
        if folder_id is not None:
            params["folderId"] = folder_id
        data = await self._request("GET", "/documents", params=params)
        items = _extract_items(data, "documents") or []
        return [CraftDocument.model_validate(item) for item in items]

    async def find_book_document(self, title: str, *, folder_id: str) -> str | None:
        """Return the id of a document with the exact title in ``folder_id``, else None."""
        for doc in await self.list_documents(folder_id=folder_id):
            if doc.title == title:
                return doc.id
        return None

    async def create_document(self, title: str, *, folder_id: str) -> str:
        """Create an empty document and return its id."""
        data = await self._request(
            "POST",
            "/documents",
            json={
                "documents": [{"title": title}],
                "destination": {"folderId": folder_id},
            },
        )
        items = _extract_items(data, "documents")
        if not items:
            raise CraftError(
                f"create_document returned no recognizable items for title={title!r}. "
                f"Raw response: {data!r}"
            )
        return CraftDocument.model_validate(items[0]).id

    # ---------------------------------------------------------------------- #
    # Blocks                                                                  #
    # ---------------------------------------------------------------------- #

    async def insert_blocks(
        self,
        *,
        document_id: str,
        blocks: list[dict],
        position: str = "end",
    ) -> None:
        """Insert one or more blocks into the document at ``position``."""
        if not blocks:
            return
        await self._request(
            "POST",
            "/blocks",
            json={
                "blocks": blocks,
                "position": {
                    "pageId": document_id,
                    "position": position,
                },
            },
        )

    async def upload_image(
        self,
        *,
        document_id: str,
        image_bytes: bytes,
        content_type: str = "image/jpeg",
        position: str = "start",
    ) -> str | None:
        """Upload an image as a new image block. Returns block id when known.

        Uses Craft's POST /upload endpoint, which stores the asset AND inserts
        a block at the requested position. The content-type drives the block
        type — ``image/jpeg`` renders as an image block, ``application/octet-
        stream`` (the docs' nominal default) stores it as a generic file
        attachment. We stick with ``image/jpeg`` and rely on the retry-with-
        fresh-connection path to handle sporadic Craft-side TLS failures.
        """
        await self._throttle()
        response = await self.http.post(
            f"{self.api_url}/upload",
            params={"pageId": document_id, "position": position},
            content=image_bytes,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": content_type,
            },
        )
        _check(response)
        if not response.content:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        if isinstance(data, dict):
            return data.get("blockId") or data.get("id")
        return None

    # ---------------------------------------------------------------------- #
    # High-level convenience                                                  #
    # ---------------------------------------------------------------------- #

    async def create_book_document(
        self,
        *,
        title: str,
        blocks: list[dict],
        folder_id: str,
    ) -> str:
        """Create the doc shell, then fill it with blocks. Returns the document id."""
        document_id = await self.create_document(title, folder_id=folder_id)
        await self.insert_blocks(document_id=document_id, blocks=blocks)
        return document_id

    async def append_to_book_document(
        self,
        *,
        document_id: str,
        blocks: list[dict],
    ) -> None:
        """Append blocks to an existing book document."""
        await self.insert_blocks(document_id=document_id, blocks=blocks, position="end")

    # ---------------------------------------------------------------------- #
    # HTTP plumbing                                                           #
    # ---------------------------------------------------------------------- #

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any:
        await self._throttle()
        response = await self.http.request(
            method,
            f"{self.api_url}{path}",
            params=params,
            json=json,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        _check(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def _throttle(self) -> None:
        async with self._throttle_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_at
            if elapsed < self.MIN_REQUEST_INTERVAL:
                await asyncio.sleep(self.MIN_REQUEST_INTERVAL - elapsed)
            self._last_request_at = time.monotonic()


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


# Plausible response wrappers Craft has used in different doc snippets.
_LIST_KEYS = ("folders", "documents", "data", "items", "results", "result", "created")


def _extract_items(data: Any, expected_key: str) -> list[dict] | None:
    """Pull a list of items out of Craft's response.

    Real-world Craft responses have shifted between top-level arrays, the
    request-key (``folders``/``documents``), and the singleton object. This
    helper tries them all, in roughly the order seen in the docs, so a small
    schema change doesn't break sync.
    """
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    # Preferred: the same key the request used
    for key in (expected_key, *_LIST_KEYS):
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [value]
    # Last resort: the response itself looks like a single item (has an id)
    if "id" in data:
        return [data]
    return None


def _check(response: httpx.Response) -> None:
    code = response.status_code
    if 200 <= code < 300:
        return
    body = response.text
    if code == 401:
        raise CraftAuthError("Unauthorized — check your Craft API token")
    if code == 403:
        raise CraftAuthError(f"Forbidden: {body[:200]}")
    if code == 404:
        raise CraftNotFound("Resource not found")
    if code == 429:
        retry_after = response.headers.get("Retry-After")
        msg = "Rate limited"
        if retry_after:
            msg = f"{msg} — retry after {retry_after}s"
        raise CraftRateLimited(msg)
    raise CraftHttpError(code, body)
