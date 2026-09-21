import os

import bcrypt

# Must be set before any `app.*` module is imported -- config.py reads these
# eagerly at import time and raises if the required ones are missing.
TEST_PASSWORD = "correct horse battery staple"
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault(
    "DASHBOARD_PASSWORD_HASH", bcrypt.hashpw(TEST_PASSWORD.encode(), bcrypt.gensalt()).decode()
)
os.environ.setdefault("SESSION_SECRET", "test-session-secret-not-for-real-use")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
# Keep the lockout test fast and deterministic regardless of the app's
# real-world defaults.
os.environ.setdefault("LOGIN_MAX_ATTEMPTS", "3")
os.environ.setdefault("LOGIN_LOCKOUT_MINUTES", "1")

import fakeredis  # noqa: E402
import pytest  # noqa: E402

from app import redis_client  # noqa: E402


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Every test gets a fresh in-memory Redis -- no real server needed, and
    no state leaks between tests (each fixture instance is brand new)."""
    fake = fakeredis.FakeAsyncRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "_pool", fake)
    yield fake
