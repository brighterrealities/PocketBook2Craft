"""Application container — wires credential storage, sync state, and clients.

One instance lives in ``app.state.container`` for the lifetime of the FastAPI
process. Routes pull it via ``Depends(get_container)``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from pb2craft.api.craft import CraftClient
from pb2craft.api.pocketbook import PocketBookClient
from pb2craft.config import Settings
from pb2craft.credentials import AppSettings, CraftConfig, CredentialFile
from pb2craft.models import SyncResult
from pb2craft.state import SyncStateStore
from pb2craft.sync import SyncService

log = logging.getLogger(__name__)


class NotConfiguredError(Exception):
    """Raised when sync is requested but credentials are missing."""


@dataclass
class SyncRunRecord:
    started_at: datetime
    finished_at: datetime
    success: bool
    books: int
    highlights: int
    skipped: int
    errors: list[str] = field(default_factory=list)
    error_message: str | None = None  # for catastrophic failures (not configured, etc.)


_SYNC_JOB_ID = "periodic_sync"


class AppContainer:
    """Holds all the singletons the web app needs."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.config_dir: Path = settings.config_dir
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.credentials = CredentialFile(self.config_dir / "credentials.json")
        self.state = SyncStateStore(self.config_dir / "state.sqlite")
        self.run_log: deque[SyncRunRecord] = deque(maxlen=20)
        self._sync_lock = asyncio.Lock()
        self._scheduler: AsyncIOScheduler | None = None

    # ---------------------------------------------------------------------- #
    # Scheduler                                                               #
    # ---------------------------------------------------------------------- #

    def current_sync_interval_minutes(self) -> int:
        """UI-persisted setting wins; env-var Settings only seeds the default."""
        persisted = self.credentials.load_settings()
        if persisted is not None:
            return persisted.sync_interval_minutes
        return self.settings.sync_interval_minutes

    def start_scheduler(self) -> None:
        """Boot the scheduler and apply the persisted/seeded interval."""
        if self._scheduler is None:
            self._scheduler = AsyncIOScheduler()
        if not self._scheduler.running:
            self._scheduler.start()
        self._apply_interval(self.current_sync_interval_minutes())

    def shutdown_scheduler(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def update_sync_interval(self, minutes: int) -> None:
        """Persist new interval and reschedule (or pause) the job in place."""
        minutes = max(0, min(minutes, 1440))  # clamp 0–24h
        self.credentials.save_settings(AppSettings(sync_interval_minutes=minutes))
        self._apply_interval(minutes)

    def _apply_interval(self, minutes: int) -> None:
        if self._scheduler is None:
            return
        # Remove any existing job so we can rebuild from scratch.
        if self._scheduler.get_job(_SYNC_JOB_ID) is not None:
            self._scheduler.remove_job(_SYNC_JOB_ID)
        if minutes <= 0:
            log.info("Scheduled sync disabled (interval=0)")
            return
        self._scheduler.add_job(
            _scheduled_sync,
            IntervalTrigger(minutes=minutes),
            args=[self],
            id=_SYNC_JOB_ID,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        log.info("Scheduled sync every %d minute(s)", minutes)

    # ---------------------------------------------------------------------- #
    # Client builders                                                         #
    # ---------------------------------------------------------------------- #

    def build_pocketbook_client(self) -> PocketBookClient:
        """Build a transient PocketBookClient using the persisted token store."""
        return PocketBookClient(token_store=self.credentials)

    def build_craft_client(self) -> CraftClient | None:
        """Build a CraftClient from persisted config, or None if not yet configured."""
        cfg = self.credentials.load_craft()
        if cfg is None:
            return None
        return CraftClient(api_url=cfg.api_url, token=cfg.api_token)

    # ---------------------------------------------------------------------- #
    # Sync                                                                    #
    # ---------------------------------------------------------------------- #

    def is_ready_to_sync(self) -> tuple[bool, str | None]:
        if self.credentials.load() is None:
            return False, "PocketBook not signed in"
        if self.credentials.load_craft() is None:
            return False, "Craft connection not configured"
        return True, None

    @property
    def is_syncing(self) -> bool:
        return self._sync_lock.locked()

    async def run_sync(self, *, force: bool = False) -> SyncRunRecord:
        """Run a sync end-to-end and record the result. Single-flighted."""
        async with self._sync_lock:
            started_at = datetime.now(tz=timezone.utc)
            ready, reason = self.is_ready_to_sync()
            if not ready:
                record = SyncRunRecord(
                    started_at=started_at,
                    finished_at=datetime.now(tz=timezone.utc),
                    success=False,
                    books=0,
                    highlights=0,
                    skipped=0,
                    error_message=reason or "Not configured",
                )
                self.run_log.appendleft(record)
                raise NotConfiguredError(reason or "Not configured")

            craft_cfg = self.credentials.load_craft()
            assert craft_cfg is not None  # guaranteed by is_ready_to_sync

            pb = self.build_pocketbook_client()
            craft = self.build_craft_client()
            assert craft is not None

            try:
                svc = SyncService(
                    pocketbook=pb,
                    craft=craft,
                    state=self.state,
                    folder_name=craft_cfg.folder_name,
                    quote_decorations=craft_cfg.quote_decorations,
                    add_author_tag=craft_cfg.add_author_tag,
                    add_publisher_tag=craft_cfg.add_publisher_tag,
                )
                result: SyncResult = await svc.sync(force=force)
                record = SyncRunRecord(
                    started_at=started_at,
                    finished_at=datetime.now(tz=timezone.utc),
                    success=not result.has_errors,
                    books=result.total_books,
                    highlights=result.total_highlights,
                    skipped=result.skipped_highlights,
                    errors=result.errors,
                )
            except Exception as e:  # noqa: BLE001 — surface any boom in the run log
                log.exception("Sync failed catastrophically")
                record = SyncRunRecord(
                    started_at=started_at,
                    finished_at=datetime.now(tz=timezone.utc),
                    success=False,
                    books=0,
                    highlights=0,
                    skipped=0,
                    error_message=str(e),
                )
            finally:
                await pb.aclose()
                await craft.aclose()

            self.run_log.appendleft(record)
            return record


async def _scheduled_sync(container: "AppContainer") -> None:
    """Scheduler entry point — wraps run_sync, swallows expected errors quietly."""
    sched_log = logging.getLogger("pb2craft.scheduler")
    try:
        await container.run_sync()
    except NotConfiguredError as e:
        sched_log.info("Skipping scheduled sync: %s", e)
    except Exception:  # noqa: BLE001
        sched_log.exception("Scheduled sync raised unexpectedly")


# --------------------------------------------------------------------------- #
# Env-var seeding (first boot only)                                            #
# --------------------------------------------------------------------------- #


def seed_from_env(container: AppContainer) -> None:
    """Populate Craft config from env vars when the persisted file lacks it.

    Only runs on first boot — subsequent boots ignore env vars in favor of
    whatever the web UI has saved.
    """
    settings = container.settings
    if container.credentials.load_craft() is not None:
        return
    if not (settings.craft_api_url and settings.craft_api_token):
        return
    container.credentials.save_craft(
        CraftConfig(
            api_url=settings.craft_api_url,
            api_token=settings.craft_api_token,
            folder_name=settings.craft_folder_name,
        )
    )
    log.info("Seeded Craft config from environment variables")
