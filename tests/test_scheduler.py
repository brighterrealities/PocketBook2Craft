"""Tests for the container-owned APScheduler integration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pb2craft import app as app_module
from pb2craft.app import AppContainer, NotConfiguredError
from pb2craft.config import Settings


@pytest.fixture
async def container(tmp_path):
    settings = Settings(config_dir=tmp_path / "config")  # type: ignore[call-arg]
    c = AppContainer(settings=settings)
    yield c
    c.shutdown_scheduler()


def test_current_interval_falls_back_to_env_seed(container):
    # No persisted settings — env-var seed (Settings default = 60)
    assert container.current_sync_interval_minutes() == 60


def test_update_sync_interval_persists_and_returns(container):
    container.update_sync_interval(15)
    assert container.current_sync_interval_minutes() == 15

    # Survives a fresh AppContainer pointed at the same dir
    fresh = AppContainer(settings=container.settings)
    try:
        assert fresh.current_sync_interval_minutes() == 15
    finally:
        fresh.shutdown_scheduler()


def test_update_sync_interval_clamps_to_range(container):
    container.update_sync_interval(-5)
    assert container.current_sync_interval_minutes() == 0
    container.update_sync_interval(9999)
    assert container.current_sync_interval_minutes() == 1440


async def test_start_scheduler_with_positive_interval_creates_job(container):
    container.update_sync_interval(30)
    container.start_scheduler()
    assert container._scheduler is not None
    assert container._scheduler.running
    assert container._scheduler.get_job("periodic_sync") is not None


async def test_start_scheduler_with_zero_interval_creates_no_job(container):
    container.update_sync_interval(0)
    container.start_scheduler()
    assert container._scheduler is not None
    assert container._scheduler.running
    assert container._scheduler.get_job("periodic_sync") is None


async def test_update_interval_to_zero_removes_running_job(container):
    container.update_sync_interval(60)
    container.start_scheduler()
    assert container._scheduler.get_job("periodic_sync") is not None

    container.update_sync_interval(0)
    assert container._scheduler.get_job("periodic_sync") is None


async def test_update_interval_reschedules_running_job(container):
    container.update_sync_interval(60)
    container.start_scheduler()
    container.update_sync_interval(15)
    # Job replaced, not duplicated
    jobs = container._scheduler.get_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "periodic_sync"


async def test_scheduled_sync_calls_container_run_sync(container, monkeypatch):
    mock_run = AsyncMock()
    monkeypatch.setattr(container, "run_sync", mock_run)

    await app_module._scheduled_sync(container)

    mock_run.assert_awaited_once_with()


async def test_scheduled_sync_swallows_not_configured(container, monkeypatch):
    async def raise_not_configured(**_):
        raise NotConfiguredError("not configured")
    monkeypatch.setattr(container, "run_sync", raise_not_configured)

    # Should not raise
    await app_module._scheduled_sync(container)


async def test_scheduled_sync_swallows_unexpected_exceptions(container, monkeypatch):
    async def boom(**_):
        raise RuntimeError("network died")
    monkeypatch.setattr(container, "run_sync", boom)

    await app_module._scheduled_sync(container)
