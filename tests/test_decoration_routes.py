"""Route tests for the decoration setting (form + update endpoint)."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from pb2craft.app import AppContainer
from pb2craft.credentials import CraftConfig
from pb2craft.main import create_app

CRAFT_URL = "https://connect.craft.do/links/TESTLINK/api/v1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PB2C_CONFIG_DIR", str(tmp_path / "config"))
    app = create_app()
    with TestClient(app) as tc:
        yield tc


def _container(client: TestClient) -> AppContainer:
    return client.app.state.container


def _seed_craft(container: AppContainer, decorations=None) -> None:
    container.credentials.save_craft(
        CraftConfig(
            api_url=CRAFT_URL,
            api_token="tok",
            folder_name="PocketBook Imports",
            quote_decorations=decorations if decorations is not None else ["quote", "callout"],
        )
    )


# --------------------------------------------------------------------------- #
# Initial Craft save — checkbox parsing                                        #
# --------------------------------------------------------------------------- #


@respx.mock
def test_craft_save_both_decorations_checked(client: TestClient):
    respx.get(f"{CRAFT_URL}/folders").mock(return_value=httpx.Response(200, json={"folders": []}))

    r = client.post(
        "/login/craft",
        data={"api_url": CRAFT_URL, "api_token": "t", "folder_name": "f",
              "deco_focus": "1", "deco_block": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    cfg = _container(client).credentials.load_craft()
    assert cfg is not None
    assert cfg.quote_decorations == ["quote", "callout"]


@respx.mock
def test_craft_save_only_focus(client: TestClient):
    respx.get(f"{CRAFT_URL}/folders").mock(return_value=httpx.Response(200, json={"folders": []}))

    r = client.post(
        "/login/craft",
        data={"api_url": CRAFT_URL, "api_token": "t", "folder_name": "f",
              "deco_focus": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _container(client).credentials.load_craft().quote_decorations == ["quote"]


@respx.mock
def test_craft_save_only_block(client: TestClient):
    respx.get(f"{CRAFT_URL}/folders").mock(return_value=httpx.Response(200, json={"folders": []}))

    r = client.post(
        "/login/craft",
        data={"api_url": CRAFT_URL, "api_token": "t", "folder_name": "f",
              "deco_block": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _container(client).credentials.load_craft().quote_decorations == ["callout"]


@respx.mock
def test_craft_save_neither_decoration(client: TestClient):
    respx.get(f"{CRAFT_URL}/folders").mock(return_value=httpx.Response(200, json={"folders": []}))

    r = client.post(
        "/login/craft",
        data={"api_url": CRAFT_URL, "api_token": "t", "folder_name": "f"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _container(client).credentials.load_craft().quote_decorations == []


# --------------------------------------------------------------------------- #
# Update endpoint — change decoration without re-entering token                #
# --------------------------------------------------------------------------- #


def test_update_craft_decorations_changes_persisted_list(client: TestClient):
    _seed_craft(_container(client), decorations=["quote", "callout"])

    r = client.post(
        "/settings/craft-display",
        data={"deco_focus": "1"},  # uncheck block
    )
    assert r.status_code == 200
    cfg = _container(client).credentials.load_craft()
    assert cfg is not None
    assert cfg.quote_decorations == ["quote"]
    assert cfg.api_token == "tok"  # unchanged


def test_update_craft_decorations_keeps_other_fields(client: TestClient):
    _seed_craft(_container(client), decorations=["quote"])

    r = client.post(
        "/settings/craft-display",
        data={"deco_focus": "1", "deco_block": "1"},
    )
    assert r.status_code == 200
    cfg = _container(client).credentials.load_craft()
    assert cfg.api_url == CRAFT_URL
    assert cfg.folder_name == "PocketBook Imports"
    assert cfg.quote_decorations == ["quote", "callout"]


def test_update_craft_decorations_when_not_configured_redirects(client: TestClient):
    """If there's no Craft config yet, redirect to /login instead of crashing."""
    r = client.post(
        "/settings/craft-display",
        data={"deco_focus": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_update_craft_decorations_shows_inline_success(client: TestClient):
    _seed_craft(_container(client))
    r = client.post("/settings/craft-display", data={"deco_focus": "1"})
    assert "Display settings updated." in r.text
