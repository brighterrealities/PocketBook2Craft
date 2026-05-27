from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Env vars are seed values for first boot; the web UI is authoritative and
    persists overrides to /config/credentials.json (created in Phase 5).
    """

    model_config = SettingsConfigDict(env_prefix="PB2C_", env_file=None, extra="ignore")

    # Storage
    config_dir: Path = Field(default=Path("/config"))

    # Web
    web_host: str = "0.0.0.0"
    web_port: int = 8080

    # Sync
    sync_interval_minutes: int = 60

    # Logging
    log_level: str = "INFO"

    # Seed values (optional; UI overrides persist to /config/credentials.json)
    pb_email: str | None = None
    pb_password: str | None = None
    craft_api_url: str | None = None
    craft_api_token: str | None = None
    craft_folder_name: str = "PocketBook Imports"


settings = Settings()
