"""Integration tests for the FastAPI routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from pb2craft.api.craft import CraftClient
from pb2craft.api.pocketbook import PocketBookClient, PocketBookCredentials
from pb2craft.app import AppContainer
from pb2craft.credentials import CraftConfig
from pb2craft.main import create_app

PB_BASE = PocketBookClient.BASE_URL
CRAFT_URL = "https://connect.craft.do/links/TESTLINK/api/v1"


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient with a fresh AppContainer pointing at tmp_path/config.

    The Settings object is read inside ``lifespan``, so setting the env var
    before the app starts is enough — no need to invalidate any cache.
    """
    monkeypatch.setenv("PB2C_CONFIG_DIR", str(tmp_path / "config"))

    app = create_app()
    with TestClient(app) as tc:
        yield tc


def _container(client: TestClient) -> AppContainer:
    return client.app.state.container


def _seed_pb(container: AppContainer) -> None:
    container.credentials.save(
        PocketBookCredentials(
            access_token="A",
            refresh_token="R",
            expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
            shop_alias="us",
        )
    )


def _seed_craft(container: AppContainer) -> None:
    container.credentials.save_craft(
        CraftConfig(api_url=CRAFT_URL, api_token="tok", folder_name="PocketBook Imports")
    )


# --------------------------------------------------------------------------- #
# Health                                                                       #
# --------------------------------------------------------------------------- #


def test_healthz_returns_ok(client: TestClient):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# --------------------------------------------------------------------------- #
# Dashboard                                                                    #
# --------------------------------------------------------------------------- #


def test_dashboard_renders_when_unconfigured(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Not signed in" in body
    assert "Not configured" in body
    assert "PocketBook2Craft" in body


def test_dashboard_shows_ready_when_both_configured(client: TestClient):
    _seed_pb(_container(client))
    _seed_craft(_container(client))

    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Signed in" in body
    assert "Configured" in body
    assert "Ready" in body


# --------------------------------------------------------------------------- #
# Login — PocketBook                                                           #
# --------------------------------------------------------------------------- #


def test_login_page_renders(client: TestClient):
    r = client.get("/login")
    assert r.status_code == 200
    assert "PocketBook Cloud" in r.text
    assert "Craft.do" in r.text


@respx.mock
def test_pocketbook_discover_shows_shops(client: TestClient):
    respx.get(f"{PB_BASE}/auth/login").mock(
        return_value=httpx.Response(
            200,
            json={"providers": [{"alias": "us", "name": "US Shop", "shop_id": "1"}]},
        )
    )

    r = client.post("/login/pocketbook/discover", data={"email": "user@example.com"})
    assert r.status_code == 200
    assert "US Shop" in r.text
    assert 'name="password"' in r.text


@respx.mock
def test_pocketbook_discover_shows_error_on_failure(client: TestClient):
    respx.get(f"{PB_BASE}/auth/login").mock(return_value=httpx.Response(403, text="bad email"))

    r = client.post("/login/pocketbook/discover", data={"email": "user@example.com"})
    assert r.status_code == 400
    assert "Forbidden" in r.text or "bad email" in r.text or "forbidden" in r.text.lower()


@respx.mock
def test_pocketbook_complete_persists_creds(client: TestClient):
    # Discover step is replayed inside the complete handler.
    respx.get(f"{PB_BASE}/auth/login").mock(
        return_value=httpx.Response(
            200,
            json={"providers": [{"alias": "us", "name": "US Shop", "shop_id": "1"}]},
        )
    )
    respx.post(f"{PB_BASE}/auth/login/us").mock(
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

    r = client.post(
        "/login/pocketbook/complete",
        data={"email": "user@example.com", "password": "pw", "shop_alias": "us"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    creds = _container(client).credentials.load()
    assert creds is not None
    assert creds.access_token == "ACCESS"


def test_pocketbook_logout_clears_creds(client: TestClient):
    _seed_pb(_container(client))
    assert _container(client).credentials.load() is not None

    r = client.post("/logout/pocketbook", follow_redirects=False)
    assert r.status_code == 303
    assert _container(client).credentials.load() is None


# --------------------------------------------------------------------------- #
# Login — Craft                                                                #
# --------------------------------------------------------------------------- #


@respx.mock
def test_craft_save_persists_after_successful_test(client: TestClient):
    respx.get(f"{CRAFT_URL}/folders").mock(
        return_value=httpx.Response(200, json={"folders": []})
    )

    r = client.post(
        "/login/craft",
        data={
            "api_url": CRAFT_URL,
            "api_token": "good-token",
            "folder_name": "PocketBook Imports",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    cfg = _container(client).credentials.load_craft()
    assert cfg is not None
    assert cfg.api_url == CRAFT_URL


@respx.mock
def test_craft_save_rejects_bad_token(client: TestClient):
    respx.get(f"{CRAFT_URL}/folders").mock(return_value=httpx.Response(401))

    r = client.post(
        "/login/craft",
        data={"api_url": CRAFT_URL, "api_token": "bad", "folder_name": "x"},
    )
    assert r.status_code == 400
    assert "Authentication failed" in r.text
    assert _container(client).credentials.load_craft() is None


def test_craft_logout_clears(client: TestClient):
    _seed_craft(_container(client))
    r = client.post("/logout/craft", follow_redirects=False)
    assert r.status_code == 303
    assert _container(client).credentials.load_craft() is None


# --------------------------------------------------------------------------- #
# Sync                                                                         #
# --------------------------------------------------------------------------- #


def test_sync_redirects_when_unconfigured(client: TestClient):
    r = client.post("/sync", follow_redirects=False)
    assert r.status_code == 303  # still 303, dashboard shows the not-ready badge


def test_sync_triggers_run_sync_when_configured(client: TestClient, monkeypatch):
    _seed_pb(_container(client))
    _seed_craft(_container(client))

    container = _container(client)
    called = {}

    async def fake_run_sync(*, force: bool = False):
        called["force"] = force
        from pb2craft.app import SyncRunRecord
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc)
        rec = SyncRunRecord(
            started_at=now, finished_at=now, success=True,
            books=2, highlights=10, skipped=0,
        )
        container.run_log.appendleft(rec)
        return rec

    monkeypatch.setattr(container, "run_sync", fake_run_sync)

    r = client.post("/sync", data={"force": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert called["force"] is True
    assert len(container.run_log) == 1


# --------------------------------------------------------------------------- #
# Logs                                                                         #
# --------------------------------------------------------------------------- #


def test_logs_page_empty(client: TestClient):
    r = client.get("/logs")
    assert r.status_code == 200
    assert "No sync runs yet" in r.text


def test_logs_page_shows_runs(client: TestClient):
    from pb2craft.app import SyncRunRecord
    container = _container(client)
    now = datetime.now(tz=timezone.utc)
    container.run_log.appendleft(
        SyncRunRecord(
            started_at=now, finished_at=now + timedelta(seconds=2),
            success=True, books=3, highlights=15, skipped=1,
        )
    )

    r = client.get("/logs")
    assert r.status_code == 200
    assert "OK" in r.text
    assert ">3<" in r.text  # books count
    assert ">15<" in r.text  # highlights count
