"""Tests for the file-backed CredentialFile."""

from __future__ import annotations

import stat
from datetime import datetime, timedelta, timezone

import pytest

from pb2craft.api.pocketbook import PocketBookCredentials
from pb2craft.credentials import CraftConfig, CredentialFile


@pytest.fixture
def cred_file(tmp_path):
    return CredentialFile(tmp_path / "credentials.json")


def test_missing_file_returns_none(cred_file: CredentialFile):
    assert cred_file.load() is None
    assert cred_file.load_craft() is None


def test_pocketbook_round_trip(cred_file: CredentialFile):
    creds = PocketBookCredentials(
        access_token="A",
        refresh_token="R",
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        shop_alias="us",
    )
    cred_file.save(creds)
    loaded = cred_file.load()
    assert loaded is not None
    assert loaded.access_token == "A"
    assert loaded.shop_alias == "us"


def test_craft_round_trip(cred_file: CredentialFile):
    cfg = CraftConfig(
        api_url="https://connect.craft.do/links/abc/api/v1",
        api_token="t",
        folder_name="My Folder",
        folder_id="f-1",
    )
    cred_file.save_craft(cfg)
    loaded = cred_file.load_craft()
    assert loaded is not None
    assert loaded.api_url == "https://connect.craft.do/links/abc/api/v1"
    assert loaded.folder_id == "f-1"


def test_file_has_600_perms(cred_file: CredentialFile):
    cfg = CraftConfig(api_url="x", api_token="y")
    cred_file.save_craft(cfg)
    mode = stat.S_IMODE(cred_file.path.stat().st_mode)
    assert mode == 0o600


def test_pocketbook_clear(cred_file: CredentialFile):
    cred_file.save(
        PocketBookCredentials(
            access_token="A",
            refresh_token="R",
            expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
            shop_alias="us",
        )
    )
    cred_file.clear()
    assert cred_file.load() is None


def test_corrupt_file_returns_none_without_raising(cred_file: CredentialFile, tmp_path):
    cred_file.path.write_text("{not valid json")
    assert cred_file.load() is None
    assert cred_file.load_craft() is None


def test_invalid_stored_creds_returns_none(cred_file: CredentialFile):
    # Write a JSON file that's parseable but doesn't match the schema
    cred_file.path.write_text('{"pocketbook": {"wrong": "shape"}}')
    assert cred_file.load() is None
