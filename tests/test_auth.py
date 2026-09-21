import time

from app import auth


class _FakeRequest:
    """auth.read_session() only ever touches request.cookies.get(...) --
    no need for a real Starlette Request."""

    def __init__(self, cookies):
        self.cookies = cookies


def test_valid_session_round_trips():
    cookie = auth.create_session_cookie("admin")
    session = auth.read_session(_FakeRequest({auth.SESSION_COOKIE: cookie}))
    assert session["u"] == "admin"


def test_missing_cookie_is_not_authenticated():
    assert auth.read_session(_FakeRequest({})) is None


def test_garbage_cookie_is_not_authenticated():
    assert auth.read_session(_FakeRequest({auth.SESSION_COOKIE: "not-a-valid-signed-cookie"})) is None


def test_idle_timeout_expires_stale_session(monkeypatch):
    # A session signed 61 seconds ago with a 60s idle window should be
    # rejected -- this is the "sign out after N minutes of inactivity"
    # behavior itself.
    monkeypatch.setattr(auth, "SESSION_IDLE_SECONDS", 60)
    cookie = auth.create_session_cookie("admin")
    real_time = time.time
    monkeypatch.setattr(auth.time, "time", lambda: real_time() + 61)
    assert auth.read_session(_FakeRequest({auth.SESSION_COOKIE: cookie})) is None


def test_absolute_cap_expires_session_even_if_recently_refreshed(monkeypatch):
    # Simulates a session that's been kept alive by constant activity (so
    # the idle window alone would never expire it) but has existed longer
    # than the absolute cap -- must still be rejected.
    monkeypatch.setattr(auth, "SESSION_IDLE_SECONDS", 60 * 60 * 24)  # generous idle window
    monkeypatch.setattr(auth, "SESSION_ABSOLUTE_SECONDS", 100)
    old_login_ts = time.time() - 200  # older than the absolute cap
    # create_session_cookie signs "now" as the itsdangerous timestamp (so
    # the idle check passes) but carries the old login_ts inside the payload
    # (so the absolute-cap check fails) -- exactly what the refresh
    # middleware produces on a long-lived, continuously-active session.
    cookie = auth.create_session_cookie("admin", login_ts=old_login_ts)
    assert auth.read_session(_FakeRequest({auth.SESSION_COOKIE: cookie})) is None


def test_require_admin_raises_when_not_authenticated():
    import pytest

    from app.auth import NotAuthenticated, require_admin

    with pytest.raises(NotAuthenticated):
        require_admin(_FakeRequest({}))
