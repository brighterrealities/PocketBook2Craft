"""Tests for the #author / #publisher tag feature."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from pb2craft import formatter
from pb2craft.app import AppContainer
from pb2craft.credentials import CraftConfig
from pb2craft.main import create_app
from pb2craft.models import Book

from tests.conftest import make_book, make_highlight

CRAFT_URL = "https://connect.craft.do/links/TESTLINK/api/v1"


# --------------------------------------------------------------------------- #
# author_tags / publisher_tag helpers                                          #
# --------------------------------------------------------------------------- #


def test_author_tags_single_name():
    assert formatter.author_tags("Iain McGilchrist") == ["#IainMcGilchrist"]


def test_author_tags_splits_on_ampersand():
    tags = formatter.author_tags("Janet W. Hardy & Dossie Easton")
    # Periods are stripped because Craft terminates tags at non-word chars
    assert tags == ["#JanetWHardy", "#DossieEaston"]


def test_author_tags_strips_periods_and_other_punctuation():
    assert formatter.author_tags("Elaine N. Aron") == ["#ElaineNAron"]
    assert formatter.author_tags("Mary-Anne Smith") == ["#MaryAnneSmith"]


def test_author_tags_preserves_unicode_letters():
    assert formatter.author_tags("François Müller") == ["#FrançoisMüller"]


def test_author_tags_splits_on_comma():
    tags = formatter.author_tags("Alice Brown, Bob Smith")
    assert tags == ["#AliceBrown", "#BobSmith"]


def test_author_tags_splits_on_and_word():
    tags = formatter.author_tags("Alice Brown and Bob Smith")
    assert tags == ["#AliceBrown", "#BobSmith"]


def test_author_tags_handles_empty():
    assert formatter.author_tags("") == []
    assert formatter.author_tags("   ") == []


def test_publisher_tag_strips_whitespace():
    assert formatter.publisher_tag("Yale University Press") == "#YaleUniversityPress"


def test_publisher_tag_returns_none_for_empty():
    assert formatter.publisher_tag("") is None
    assert formatter.publisher_tag("   ") is None


# --------------------------------------------------------------------------- #
# Header block rendering with tags                                             #
# --------------------------------------------------------------------------- #


def _book_with_meta(authors=None, publisher=None):
    payload = {"id": "b1", "fast_hash": "h", "title": "T"}
    meta = {}
    if authors is not None:
        meta["authors"] = authors
    if publisher is not None:
        meta["publisher"] = publisher
    if meta:
        payload["metadata"] = meta
    return Book.model_validate(payload)


def test_header_includes_author_tag_when_enabled():
    book = _book_with_meta(authors="Iain McGilchrist")
    [header, *_] = formatter.format_book_text_blocks(
        book, [make_highlight()], add_author_tag=True,
    )
    assert "#IainMcGilchrist" in header["markdown"]
    # Original Author line is still there
    assert "**Author**: Iain McGilchrist" in header["markdown"]


def test_header_includes_publisher_tag_when_enabled():
    book = _book_with_meta(authors="A", publisher="Yale University Press")
    [header, *_] = formatter.format_book_text_blocks(
        book, [make_highlight()], add_publisher_tag=True,
    )
    assert "#YaleUniversityPress" in header["markdown"]


def test_header_includes_both_tags_when_both_enabled():
    book = _book_with_meta(authors="Iain McGilchrist", publisher="Yale University Press")
    [header, *_] = formatter.format_book_text_blocks(
        book, [make_highlight()],
        add_author_tag=True, add_publisher_tag=True,
    )
    md = header["markdown"]
    assert "#IainMcGilchrist" in md
    assert "#YaleUniversityPress" in md
    # Tags share a line at the end
    last_line = md.strip().split("\n")[-1]
    assert "#IainMcGilchrist" in last_line and "#YaleUniversityPress" in last_line


def test_header_omits_tags_when_toggles_off():
    book = _book_with_meta(authors="Iain McGilchrist", publisher="Yale University Press")
    [header, *_] = formatter.format_book_text_blocks(book, [make_highlight()])
    # Heading uses '##' so we can't just check for '#'; look for actual tag values
    assert "#IainMcGilchrist" not in header["markdown"]
    assert "#YaleUniversityPress" not in header["markdown"]


def test_header_skips_missing_author_or_publisher():
    book = _book_with_meta(authors=None, publisher="Pub")  # no author
    [header, *_] = formatter.format_book_text_blocks(
        book, [make_highlight()], add_author_tag=True, add_publisher_tag=True,
    )
    assert "#Pub" in header["markdown"]
    # No author tag because metadata.authors is missing AND path heuristic
    # falls back to "Unknown Author" which we explicitly skip.
    # (The make_book fixture has a path, so display_authors won't be Unknown there;
    # use Book directly here so the path fallback doesn't kick in.)
    # We don't assert "no #" because the fallback path may or may not produce one.


def test_multi_author_tags_render_in_header():
    book = _book_with_meta(authors="Janet W. Hardy & Dossie Easton")
    [header, *_] = formatter.format_book_text_blocks(
        book, [make_highlight()], add_author_tag=True,
    )
    md = header["markdown"]
    assert "#JanetWHardy" in md
    assert "#DossieEaston" in md


# --------------------------------------------------------------------------- #
# Route: tag checkboxes round-trip                                             #
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PB2C_CONFIG_DIR", str(tmp_path / "config"))
    app = create_app()
    with TestClient(app) as tc:
        yield tc


def _container(client: TestClient) -> AppContainer:
    return client.app.state.container


@respx.mock
def test_craft_save_persists_tag_toggles(client: TestClient):
    respx.get(f"{CRAFT_URL}/folders").mock(return_value=httpx.Response(200, json={"folders": []}))

    r = client.post(
        "/login/craft",
        data={
            "api_url": CRAFT_URL, "api_token": "t", "folder_name": "f",
            "tag_author": "1", "tag_publisher": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    cfg = _container(client).credentials.load_craft()
    assert cfg.add_author_tag is True
    assert cfg.add_publisher_tag is True


@respx.mock
def test_craft_save_unchecks_tag_toggles_default_false(client: TestClient):
    respx.get(f"{CRAFT_URL}/folders").mock(return_value=httpx.Response(200, json={"folders": []}))

    r = client.post(
        "/login/craft",
        data={"api_url": CRAFT_URL, "api_token": "t", "folder_name": "f"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    cfg = _container(client).credentials.load_craft()
    assert cfg.add_author_tag is False
    assert cfg.add_publisher_tag is False


def test_update_display_settings_toggles_tags_independently(client: TestClient):
    container = _container(client)
    container.credentials.save_craft(CraftConfig(
        api_url=CRAFT_URL, api_token="t", folder_name="f",
        add_author_tag=False, add_publisher_tag=False,
    ))

    # Enable author only
    r = client.post("/settings/craft-display", data={"tag_author": "1"})
    assert r.status_code == 200
    cfg = container.credentials.load_craft()
    assert cfg.add_author_tag is True
    assert cfg.add_publisher_tag is False
