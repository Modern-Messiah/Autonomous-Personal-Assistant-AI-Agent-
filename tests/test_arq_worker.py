"""Unit tests for the ARQ worker settings builders.

``scheduler.arq_worker`` resolves settings at class-definition (import) time, so
the module is imported inside a fixture that provides the required environment —
CI has no .env, and env vars override .env values locally, keeping this
deterministic in both places.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from config.settings import get_settings

REQUIRED_ENV = {
    "DB__HOST": "localhost",
    "DB__NAME": "krisha_test",
    "DB__USER": "krisha",
    "DB__PASSWORD": "test-password",
    "REDIS__HOST": "localhost",
    "TELEGRAM__BOT_TOKEN": "42:test-token",
    "API__TWO_GIS_API_KEY": "test-2gis",
    "API__DEEPSEEK_API_KEY": "test-deepseek",
}


@pytest.fixture
def arq_worker(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    module = importlib.import_module("scheduler.arq_worker")
    yield module
    # do not leak the fake-env settings into other tests
    get_settings.cache_clear()


def _fake_settings(**overrides: Any) -> Any:
    scheduler = SimpleNamespace(
        canary_enabled=overrides.get("canary_enabled", False),
        canary_interval_hours=overrides.get("canary_interval_hours", 6),
    )
    redis = SimpleNamespace(
        host=overrides.get("host", "redis.internal"),
        port=overrides.get("port", 6380),
        db=overrides.get("db", 3),
        password=overrides.get("password"),
    )
    return SimpleNamespace(scheduler=scheduler, redis=redis)


def test_canary_cron_disabled_yields_no_jobs(
    arq_worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        arq_worker, "get_settings", lambda: _fake_settings(canary_enabled=False)
    )
    assert arq_worker.build_canary_cron_jobs() == []


def test_canary_cron_interval_maps_to_hour_set(
    arq_worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        arq_worker,
        "get_settings",
        lambda: _fake_settings(canary_enabled=True, canary_interval_hours=6),
    )

    jobs = arq_worker.build_canary_cron_jobs()

    assert len(jobs) == 1
    job = jobs[0]
    # every 6 hours starting at midnight, and once right at worker startup
    assert job.hour == {0, 6, 12, 18}
    assert job.minute == 0
    assert job.run_at_startup is True


def test_worker_redis_settings_with_and_without_password(
    arq_worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        arq_worker,
        "get_settings",
        lambda: _fake_settings(password=SecretStr("redis-secret")),
    )
    with_password = arq_worker.build_worker_redis_settings()
    assert with_password.host == "redis.internal"
    assert with_password.port == 6380
    assert with_password.database == 3
    assert with_password.password == "redis-secret"

    monkeypatch.setattr(
        arq_worker, "get_settings", lambda: _fake_settings(password=None)
    )
    without_password = arq_worker.build_worker_redis_settings()
    assert without_password.password is None


def test_worker_settings_wires_monitor_job(arq_worker: ModuleType) -> None:
    from scheduler.jobs import process_monitor_target_job, worker_shutdown, worker_startup

    settings_class = arq_worker.WorkerSettings
    assert settings_class.functions == [process_monitor_target_job]
    assert settings_class.on_startup is worker_startup
    assert settings_class.on_shutdown is worker_shutdown
    # one scraping job at a time — the arq default of 10 concurrent jobs would
    # burst krisha from one IP
    assert settings_class.max_jobs == 1
