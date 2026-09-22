"""Pin the test API key so the suite never depends on the developer's real .env."""

import os

import pytest

TEST_API_KEY = "test-api-key-not-a-secret-0123456789"   # >= 32 chars, like a real one

# Must be set before app.settings is imported anywhere, since Settings() is module-level.
os.environ["API_KEY"] = TEST_API_KEY
# Nothing listens here: the per-person rate limit fails open at once instead of
# waiting on a DNS lookup for the compose hostname "redis".
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:1/0")


@pytest.fixture(autouse=True)
def _pinned_api_key(monkeypatch):
    from app import settings as settings_module

    monkeypatch.setenv("API_KEY", TEST_API_KEY)
    settings_module.get_settings.cache_clear()
    monkeypatch.setattr(settings_module.settings, "api_key", TEST_API_KEY)
    yield
    settings_module.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_address_guard(monkeypatch):
    """Fetch tests mock hosts like acme.com at the transport; resolving them is not
    the point and would make the suite depend on the network. test_fetch turns the
    guard back on for the tests that are about it."""
    from app.settings import settings

    monkeypatch.setattr(settings, "fetch_guard_private_addresses", False)


@pytest.fixture
def api_key() -> str:
    return TEST_API_KEY
