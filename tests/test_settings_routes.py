"""Tests for the /settings page (sync interval form)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pb2craft.credentials import AppSettings, CredentialFile
from pb2craft.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PB2C_CONFIG_DIR", str(tmp_path / "config"))
    app = create_app()
    with TestClient(app) as tc:
        yield tc


def test_settings_round_trip_in_credentials_file(tmp_path):
    cred = CredentialFile(tmp_path / "credentials.json")
    assert cred.load_settings() is None  # fresh

    cred.save_settings(AppSettings(sync_interval_minutes=30))
    loaded = cred.load_settings()
    assert loaded is not None
    assert loaded.sync_interval_minutes == 30


def test_settings_page_renders(client: TestClient):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Sync interval" in r.text


def test_settings_page_shows_current_value(client: TestClient):
    container = client.app.state.container
    container.update_sync_interval(15)
    r = client.get("/settings")
    # Number input value
    assert 'value="15"' in r.text


def test_post_settings_persists_and_reschedules(client: TestClient):
    container = client.app.state.container
    # Start with 60 (env default)
    assert container.current_sync_interval_minutes() == 60

    r = client.post("/settings", data={"sync_interval_minutes": "30"})
    assert r.status_code == 200
    assert "Saved." in r.text
    assert container.current_sync_interval_minutes() == 30

    # Scheduler now has a job with the new interval
    job = container._scheduler.get_job("periodic_sync")
    assert job is not None


def test_post_settings_zero_disables_scheduler(client: TestClient):
    container = client.app.state.container
    container.update_sync_interval(60)
    assert container._scheduler.get_job("periodic_sync") is not None

    r = client.post("/settings", data={"sync_interval_minutes": "0"})
    assert r.status_code == 200
    assert container.current_sync_interval_minutes() == 0
    assert container._scheduler.get_job("periodic_sync") is None


def test_post_settings_clamps_out_of_range_values(client: TestClient):
    container = client.app.state.container

    client.post("/settings", data={"sync_interval_minutes": "-50"})
    assert container.current_sync_interval_minutes() == 0

    client.post("/settings", data={"sync_interval_minutes": "9999"})
    assert container.current_sync_interval_minutes() == 1440


def test_settings_survives_app_restart(tmp_path, monkeypatch):
    """A persisted setting wins over the env-var seed on next boot."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("PB2C_CONFIG_DIR", str(config_dir))

    # First boot: persist 20
    app1 = create_app()
    with TestClient(app1) as tc1:
        tc1.post("/settings", data={"sync_interval_minutes": "20"})

    # Second boot: same env, persisted setting should win
    app2 = create_app()
    with TestClient(app2) as tc2:
        container = tc2.app.state.container
        assert container.current_sync_interval_minutes() == 20


def test_nav_links_to_settings(client: TestClient):
    r = client.get("/")
    assert 'href="/settings"' in r.text
