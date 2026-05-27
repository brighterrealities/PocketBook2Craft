"""File-backed credential store.

Persists PocketBook OAuth tokens and Craft connection details to a single
``credentials.json`` file on the ``/config`` volume. The file is written
atomically and chmoded to ``0600`` so only the container user can read it.

Implements the :class:`TokenStore` Protocol for PocketBook auth, plus
explicit getters/setters for the Craft config block.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from pydantic import BaseModel, Field

from pb2craft.api.pocketbook import PocketBookCredentials

log = logging.getLogger(__name__)


class CraftConfig(BaseModel):
    """Persistent Craft connection state, written by the web UI."""

    api_url: str
    api_token: str
    folder_name: str = "PocketBook Imports"
    folder_id: str | None = None
    # Craft text-block decoration values applied to each highlight quote.
    # ["quote"] = Focus (vertical bar); ["callout"] = Block (surround box).
    # Stacking is allowed and what users get by default.
    quote_decorations: list[str] = Field(default_factory=lambda: ["quote", "callout"])
    # Append #author / #publisher tags to the book header for Craft tag filtering.
    add_author_tag: bool = False
    add_publisher_tag: bool = False


class AppSettings(BaseModel):
    """Global runtime settings — UI-editable, persisted in credentials.json."""

    # 0 disables the scheduled sync (manual only); positive values are the
    # interval in minutes. The web UI clamps to 0–1440 (24h).
    sync_interval_minutes: int = 60


class CredentialFile:
    """JSON-backed store on disk with strict permissions and atomic writes.

    File layout::

        {
          "pocketbook": { ... PocketBookCredentials ... } | null,
          "craft":      { ... CraftConfig ... } | null
        }

    Missing keys are tolerated (return None). Writes are atomic to avoid a
    partial file if the container is killed mid-write.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ---------------------------------------------------------------------- #
    # PocketBook tokens (implements TokenStore Protocol)                      #
    # ---------------------------------------------------------------------- #

    def load(self) -> PocketBookCredentials | None:
        data = self._read()
        pb = data.get("pocketbook")
        if not pb:
            return None
        try:
            return PocketBookCredentials.model_validate(pb)
        except Exception as e:
            log.warning("Stored PocketBook credentials are invalid; discarding: %s", e)
            return None

    def save(self, creds: PocketBookCredentials) -> None:
        with self._lock:
            data = self._read()
            data["pocketbook"] = json.loads(creds.model_dump_json())
            self._write(data)

    def clear(self) -> None:
        with self._lock:
            data = self._read()
            data["pocketbook"] = None
            self._write(data)

    # ---------------------------------------------------------------------- #
    # Craft config                                                            #
    # ---------------------------------------------------------------------- #

    def load_craft(self) -> CraftConfig | None:
        data = self._read()
        c = data.get("craft")
        if not c:
            return None
        try:
            return CraftConfig.model_validate(c)
        except Exception as e:
            log.warning("Stored Craft config is invalid; discarding: %s", e)
            return None

    def save_craft(self, config: CraftConfig) -> None:
        with self._lock:
            data = self._read()
            data["craft"] = json.loads(config.model_dump_json())
            self._write(data)

    def clear_craft(self) -> None:
        with self._lock:
            data = self._read()
            data["craft"] = None
            self._write(data)

    # ---------------------------------------------------------------------- #
    # App settings                                                            #
    # ---------------------------------------------------------------------- #

    def load_settings(self) -> AppSettings | None:
        """Return persisted settings, or None if never saved.

        Returning None lets callers fall through to env-var defaults on first
        boot — once the UI saves anything, that wins.
        """
        data = self._read()
        s = data.get("settings")
        if not s:
            return None
        try:
            return AppSettings.model_validate(s)
        except Exception as e:
            log.warning("Stored settings are invalid; ignoring: %s", e)
            return None

    def save_settings(self, settings: AppSettings) -> None:
        with self._lock:
            data = self._read()
            data["settings"] = json.loads(settings.model_dump_json())
            self._write(data)

    # ---------------------------------------------------------------------- #
    # File plumbing                                                           #
    # ---------------------------------------------------------------------- #

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Credentials file unreadable (%s); starting fresh", e)
            return {}

    def _write(self, data: dict) -> None:
        """Atomic write with 0600 perms.

        Use ``mkstemp`` in the same directory so the final ``rename`` is on the
        same filesystem (avoids EXDEV).
        """
        fd, tmp_path_str = tempfile.mkstemp(
            prefix=".credentials.", suffix=".tmp", dir=self.path.parent
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            tmp_path.chmod(0o600)
            tmp_path.replace(self.path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
