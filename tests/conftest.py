"""Pin the test API key so the suite never depends on the developer's real .env."""

import os

import pytest

TEST_API_KEY = "test-api-key-not-a-secret"

# Must be set before app.settings is imported anywhere, since Settings() is module-level.
os.environ["API_KEY"] = TEST_API_KEY


@pytest.fixture(autouse=True)
def _pinned_api_key(monkeypatch):
    from app import settings as settings_module

    monkeypatch.setenv("API_KEY", TEST_API_KEY)
    settings_module.get_settings.cache_clear()
    monkeypatch.setattr(settings_module.settings, "api_key", TEST_API_KEY)
    yield
    settings_module.get_settings.cache_clear()


@pytest.fixture
def api_key() -> str:
    return TEST_API_KEY
