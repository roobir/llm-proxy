import time

import bcrypt
from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config

SESSION_COOKIE = "llm_proxy_session"

# Sliding idle timeout: any authenticated request re-signs the cookie (see
# main.py's refresh middleware), so this is really "time since the *last*
# request", not time since login. login_ts is carried forward unchanged
# across those re-signs and enforces a hard cap activity can't extend --
# together these answer "sign me out if I walk away" (idle) and "sign me
# out eventually no matter what" (absolute), which a single itsdangerous
# max_age can't do alone since re-signing on every request would otherwise
# make a session live forever as long as it's used.
SESSION_IDLE_SECONDS = config.SESSION_IDLE_MINUTES * 60
SESSION_ABSOLUTE_SECONDS = config.SESSION_ABSOLUTE_HOURS * 60 * 60

_serializer = URLSafeTimedSerializer(config.SESSION_SECRET, salt="llm-proxy-admin-session")


class NotAuthenticated(Exception):
    """Raised by require_admin(); caught by main.py's exception handler and
    turned into a redirect to /admin/login."""


def verify_password(password: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), config.DASHBOARD_PASSWORD_HASH.encode())
    except ValueError:
        return False


def create_session_cookie(username: str, login_ts: float | None = None) -> str:
    return _serializer.dumps({"u": username, "login_ts": login_ts if login_ts is not None else time.time()})


def read_session(request: Request) -> dict | None:
    """Returns {"u": username, "login_ts": float} if the cookie is present,
    correctly signed, within the idle window, and within the absolute cap --
    None otherwise. Never raises."""
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    try:
        data = _serializer.loads(cookie, max_age=SESSION_IDLE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    login_ts = data.get("login_ts") or 0
    if time.time() - login_ts > SESSION_ABSOLUTE_SECONDS:
        return None
    return data


def require_admin(request: Request) -> str:
    session = read_session(request)
    if not session:
        raise NotAuthenticated()
    return session["u"]
