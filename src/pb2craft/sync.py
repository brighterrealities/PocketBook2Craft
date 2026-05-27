"""Sync orchestrator — port of Swift SyncService.swift with Craft-specific upgrades.

Per-book flow:

1. Fetch highlights from PocketBook.
2. Merge split highlights (Phase 2 logic).
3. Look up state for this book:
     - If we have a recorded ``craft_doc_id`` → append-only diff against
       :class:`SyncStateStore`'s synced_highlights table.
     - If no record → check Craft folder for a doc with the matching title:
         - Found → re-link the state without re-uploading (covers the
           "/config volume got nuked" recovery case).
         - Not found → create a fresh document and upload all highlights.
4. Record everything we just pushed in SQLite.

Each book is wrapped in try/except so one failure does not poison the rest of
the run.
"""

from __future__ import annotations

import asyncio
import logging

from pb2craft.api.craft import CraftClient, CraftError
from pb2craft.api.pocketbook import PocketBookClient, PocketBookError
from pb2craft.formatter import (
    book_url,
    format_book_text_blocks,
    format_highlight_text_blocks,
)
from pb2craft.models import Book, Highlight, SyncResult
from pb2craft.processing.merger import HighlightMerger
from pb2craft.state import SyncStateStore

log = logging.getLogger(__name__)


class SyncService:
    def __init__(
        self,
        *,
        pocketbook: PocketBookClient,
        craft: CraftClient,
        state: SyncStateStore,
        folder_name: str = "PocketBook Imports",
        quote_decorations: list[str] | None = None,
        add_author_tag: bool = False,
        add_publisher_tag: bool = False,
        merger: HighlightMerger | None = None,
    ):
        self.pocketbook = pocketbook
        self.craft = craft
        self.state = state
        self.folder_name = folder_name
        # None → no decorations field; [] also → no field; non-empty list passed through
        self.quote_decorations = quote_decorations
        self.add_author_tag = add_author_tag
        self.add_publisher_tag = add_publisher_tag
        self.merger = merger or HighlightMerger()
        # title → craft_doc_id cache, populated lazily during a single sync run
        self._existing_docs: dict[str, str] | None = None

    async def sync(self, *, force: bool = False) -> SyncResult:
        """Run a full sync. ``force=True`` resets local state first.

        Note: force does NOT re-upload anything to Craft. After the reset, the
        next sync rebuilds state by finding existing docs by title — only
        genuinely new highlights get uploaded.
        """
        if force:
            log.info("Force sync requested — resetting local state")
            self.state.reset()

        # Fresh cache for this run — picks up any docs created/deleted outside us.
        self._existing_docs = None

        folder_id = await self._ensure_folder()

        books = await self.pocketbook.get_books()
        log.info("Fetched %d book(s) from PocketBook", len(books))

        result = SyncResult()

        for book in books:
            try:
                await self._sync_book(book, folder_id=folder_id, result=result)
            except (PocketBookError, CraftError) as e:
                msg = f"Failed to sync {book.display_title!r}: {e}"
                log.warning(msg)
                result.errors.append(msg)
            except Exception as e:  # noqa: BLE001 — last-resort guard so one book can't crash the run
                msg = f"Unexpected error syncing {book.display_title!r}: {e}"
                log.exception(msg)
                result.errors.append(msg)

        self.state.mark_sync_complete()
        log.info(
            "Sync complete: %d book(s) updated, %d highlight(s) pushed, %d error(s)",
            result.total_books,
            result.total_highlights,
            len(result.errors),
        )
        return result

    # ---------------------------------------------------------------------- #
    # Folder                                                                  #
    # ---------------------------------------------------------------------- #

    async def _ensure_folder(self) -> str:
        folder_id = self.state.get_folder_id()
        if folder_id:
            return folder_id
        folder_id = await self.craft.ensure_folder(self.folder_name)
        self.state.set_folder_id(folder_id)
        return folder_id

    # ---------------------------------------------------------------------- #
    # Per-book                                                                #
    # ---------------------------------------------------------------------- #

    async def _sync_book(self, book: Book, *, folder_id: str, result: SyncResult) -> None:
        # Cheapest fast-path: we examined this book at this exact mtime before
        # AND nothing on PocketBook's side has changed. Skip everything.
        if book.mtime and self.state.get_seen_mtime(book.id) == book.mtime:
            log.debug("Skipping %r (mtime unchanged: %s)", book.display_title, book.mtime)
            return

        synced_book = self.state.get_book(book.id)

        if synced_book is not None:
            # Verify the recorded Craft doc still exists in the folder. If the user
            # deleted it (or moved it out), drop back to first-time so we re-link
            # by title or create a fresh doc.
            existing_docs = await self._load_existing_docs(folder_id)
            if synced_book.craft_doc_id not in existing_docs.values():
                log.info(
                    "Recorded Craft doc for %r is missing from folder — recreating",
                    book.display_title,
                )
                synced_book = None

        highlights = await self.pocketbook.get_highlights(book)
        if not highlights:
            # Mark the book as seen so the next sync can short-circuit. Books
            # without highlights still pay one /notes call without this.
            self.state.mark_book_seen(book.id, book.mtime)
            return

        merged = self.merger.merge(highlights)

        if synced_book is None:
            await self._first_time_sync(book, merged, folder_id=folder_id, result=result)
        else:
            await self._incremental_sync(book, merged, synced_book_doc_id=synced_book.craft_doc_id,
                                          existing_count=synced_book.highlight_count, result=result)

        # Record this book's current mtime so subsequent syncs can skip it
        # entirely until PocketBook reports a change.
        self.state.mark_book_seen(book.id, book.mtime)

    async def _first_time_sync(
        self,
        book: Book,
        highlights: list[Highlight],
        *,
        folder_id: str,
        result: SyncResult,
    ) -> None:
        title = book.display_title

        # Recovery path: state lost but doc may already exist in folder.
        existing_docs = await self._load_existing_docs(folder_id)
        existing_id = existing_docs.get(title)
        if existing_id is not None:
            log.info("Re-linking existing Craft doc for %r (id=%s)", title, existing_id)
            self.state.mark_book_synced(
                book_id=book.id,
                craft_doc_id=existing_id,
                title=title,
                highlight_count=len(highlights),
                last_known_mtime=book.mtime,
            )
            self.state.mark_highlights_synced(
                book_id=book.id,
                highlight_uuids=[h.uuid for h in highlights],
            )
            # No re-upload — assume the existing doc already has these highlights.
            return

        # Fresh create: create shell, upload cover (best-effort), then fill blocks.
        # We use plain text blocks (not cards) — Craft's card block doesn't accept
        # inline <highlight> tags in the position we'd need. The card-aware block
        # builders in formatter.format_book_blocks / format_highlight_blocks are
        # kept for a future iteration that uses the two-step parentBlockId model.
        doc_id = await self.craft.create_document(title, folder_id=folder_id)
        await self._try_upload_cover(book, doc_id)

        blocks = format_book_text_blocks(
            book,
            highlights,
            decorations=self.quote_decorations,
            add_author_tag=self.add_author_tag,
            add_publisher_tag=self.add_publisher_tag,
        )
        await self.craft.insert_blocks(document_id=doc_id, blocks=blocks, position="end")

        existing_docs[title] = doc_id  # keep cache consistent for the rest of this run
        self.state.mark_book_synced(
            book_id=book.id,
            craft_doc_id=doc_id,
            title=title,
            highlight_count=len(highlights),
            last_known_mtime=book.mtime,
        )
        self.state.mark_highlights_synced(
            book_id=book.id,
            highlight_uuids=[h.uuid for h in highlights],
        )
        result.total_books += 1
        result.total_highlights += len(highlights)
        log.info("Created %r in Craft with %d highlight(s)", title, len(highlights))
        _ = book_url  # reserved for future use (e.g. linking back)

    async def _try_upload_cover(self, book: Book, document_id: str) -> None:
        """Best-effort cover upload: log warnings on failure, never raise.

        Craft's ``/upload`` endpoint sporadically returns transient TLS errors
        (e.g. ``SSLV3_ALERT_BAD_RECORD_MAC``) on connection reuse, so we retry
        once before giving up.
        """
        cover_url = book.cover_url
        if not cover_url:
            return
        try:
            image_bytes = await self.pocketbook.download_cover(cover_url)
        except Exception as e:  # noqa: BLE001 — best-effort, never abort the book
            log.warning("Failed to download cover for %r: %s", book.display_title, e)
            return

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                await self.craft.upload_image(
                    document_id=document_id,
                    image_bytes=image_bytes,
                    position="start",
                )
                return  # success
            except Exception as e:  # noqa: BLE001
                last_error = e
                if attempt == 0:
                    log.warning(
                        "Cover upload for %r failed (%s); retrying with fresh connection",
                        book.display_title,
                        e,
                    )
                    # Drop the pooled TLS connection — it's in a bad state.
                    # The next request lazy-creates a new one.
                    await self.craft.reset_connections()
                    await asyncio.sleep(1.0)
        log.warning(
            "Failed to upload cover for %r to Craft after retry: %s",
            book.display_title,
            last_error,
        )

    async def _load_existing_docs(self, folder_id: str) -> dict[str, str]:
        """Fetch the folder's documents once per sync, cache title → id."""
        if self._existing_docs is None:
            docs = await self.craft.list_documents(folder_id=folder_id)
            self._existing_docs = {d.title: d.id for d in docs}
            log.info("Loaded %d existing Craft doc(s) from folder", len(self._existing_docs))
        return self._existing_docs

    async def _incremental_sync(
        self,
        book: Book,
        highlights: list[Highlight],
        *,
        synced_book_doc_id: str,
        existing_count: int,
        result: SyncResult,
    ) -> None:
        new_highlights = self.state.filter_unsynced(book_id=book.id, highlights=highlights)
        if not new_highlights:
            result.skipped_highlights += len(highlights)
            # Still update the recorded mtime — otherwise the next sync's
            # mtime fast-path stays disabled forever for previously-synced books.
            self.state.mark_book_synced(
                book_id=book.id,
                craft_doc_id=synced_book_doc_id,
                title=book.display_title,
                highlight_count=existing_count,
                last_known_mtime=book.mtime,
            )
            return

        blocks = format_highlight_text_blocks(
            new_highlights,
            decorations=self.quote_decorations,
            start_index=existing_count + 1,
        )
        await self.craft.append_to_book_document(
            document_id=synced_book_doc_id,
            blocks=blocks,
        )
        self.state.mark_highlights_synced(
            book_id=book.id,
            highlight_uuids=[h.uuid for h in new_highlights],
        )
        self.state.mark_book_synced(
            book_id=book.id,
            craft_doc_id=synced_book_doc_id,
            title=book.display_title,
            highlight_count=existing_count + len(new_highlights),
            last_known_mtime=book.mtime,
        )
        result.total_books += 1
        result.total_highlights += len(new_highlights)
        result.skipped_highlights += len(highlights) - len(new_highlights)
        log.info(
            "Appended %d new highlight(s) to existing doc for %r",
            len(new_highlights),
            book.display_title,
        )
