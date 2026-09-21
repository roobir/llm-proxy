from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import acl, auth, config, gateway, redis_client
from .proxy import router as proxy_router
from .selftest import run_selftest
from .status import router as status_router

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="LLM Proxy")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(proxy_router)
app.include_router(status_router)


def _client_ip(request: Request) -> str:
    # Trusts the first hop's X-Forwarded-For if present -- fine for a
    # self-hosted app sitting behind a reverse proxy the operator controls;
    # falls back to the direct socket peer otherwise. This only gates the
    # login-lockout counter, not anything security-critical on its own.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.exception_handler(auth.NotAuthenticated)
async def _not_authenticated(request: Request, exc: auth.NotAuthenticated):
    return RedirectResponse("/admin/login", status_code=303)


@app.middleware("http")
async def _refresh_session(request: Request, call_next):
    # Sliding idle timeout: every request from a session that's still valid
    # re-signs the cookie with a fresh timestamp, extending the idle window
    # -- but login_ts travels forward unchanged inside the payload, so
    # auth.read_session()'s absolute-cap check still fires eventually no
    # matter how active the session is. Skipped on /admin/logout so a
    # logout's delete_cookie() isn't immediately undone by this re-adding
    # the (still-valid-at-request-time) cookie the browser just sent.
    response = await call_next(request)
    if request.url.path == "/admin/logout":
        return response
    session = auth.read_session(request)
    if session:
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.create_session_cookie(session["u"], session["login_ts"]),
            httponly=True,
            samesite="lax",
            max_age=auth.SESSION_ABSOLUTE_SECONDS,
        )
    return response


@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse("/admin")


@app.get("/favicon.ico")
async def favicon():
    # Browsers request this fixed path directly (bookmarking, some tab-icon
    # lookups) regardless of the <link rel="icon"> in each template's
    # <head> -- redirect rather than 404 so there's always a tab icon.
    return RedirectResponse("/static/favicon.svg")


# --- Admin GUI ---------------------------------------------------------
# Deliberately a separate control plane from the /v1/... ACL below: this is
# the one operator (single admin account, bcrypt + signed cookie).

@app.get("/admin/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/admin/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = _client_ip(request)
    if await redis_client.login_failure_count(ip) >= config.LOGIN_MAX_ATTEMPTS:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Too many failed attempts -- try again in a few minutes."},
            status_code=429,
        )

    if username != config.DASHBOARD_USERNAME or not auth.verify_password(password):
        await redis_client.register_login_failure(ip, config.LOGIN_LOCKOUT_MINUTES * 60)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid credentials"}, status_code=401
        )

    await redis_client.clear_login_failures(ip)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(
        auth.SESSION_COOKIE,
        auth.create_session_cookie(username),
        httponly=True,
        samesite="lax",
        max_age=auth.SESSION_ABSOLUTE_SECONDS,
    )
    return resp


@app.post("/admin/logout")
async def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@app.get("/admin", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = auth.require_admin(request)
    gw = await gateway.get_gateway_config()
    status = await redis_client.get_status(gw["default_model"] if gw else "")
    keys = await acl.list_keys()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"user": user, "status": status, "keys": keys, "new_key": None, "gateway": gw},
    )


@app.post("/admin/selftest/run")
async def selftest_run(request: Request):
    auth.require_admin(request)
    ok, detail = await run_selftest()
    await redis_client.set_selftest_result(ok, detail)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/model")
async def set_model(request: Request, model: str = Form(...)):
    # Free-text, not a dropdown -- deliberately not hardcoding a list of
    # model IDs here, since which ones are actually enabled/available can
    # change per provider and a stale guessed list would silently point
    # requests at a model that no longer works. Takes effect immediately for
    # the *next* request on either replica (both read this from Redis on
    # every call) -- no redeploy needed to switch models.
    auth.require_admin(request)
    await redis_client.set_current_model(model.strip())
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/keys/issue", response_class=HTMLResponse)
async def issue_key(request: Request, label: str = Form(...)):
    user = auth.require_admin(request)
    key_id, raw_token = await acl.issue_key(label)
    gw = await gateway.get_gateway_config()
    status = await redis_client.get_status(gw["default_model"] if gw else "")
    keys = await acl.list_keys()
    # The raw token is only ever shown here, once -- only its hash is
    # stored, so there's no "view token again" later, same as most AI
    # gateway providers' own token-issuing UX.
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "status": status,
            "keys": keys,
            "gateway": gw,
            "new_key": {"id": key_id, "label": label, "token": raw_token},
        },
    )


@app.post("/admin/keys/{key_id}/revoke")
async def revoke_key(request: Request, key_id: str):
    auth.require_admin(request)
    await acl.revoke_key(key_id)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/keys/{key_id}/delete")
async def delete_key_route(request: Request, key_id: str):
    auth.require_admin(request)
    await acl.delete_key(key_id)
    return RedirectResponse("/admin", status_code=303)


# --- Settings: which AI gateway/provider this proxy forwards to -----------

@app.get("/admin/settings", response_class=HTMLResponse)
async def settings_form(request: Request, edit_alias: str | None = None):
    user = auth.require_admin(request)
    gw = await gateway.get_gateway_config() or dict(gateway.EMPTY_GATEWAY)
    # ?edit_alias=<alias> pre-fills the alias form for editing, same as the
    # AI gateway form above always does -- without this, correcting an
    # alias's base_url/model/headers meant retyping everything from scratch
    # (only the alias name and api_key had any "keep existing" affordance).
    alias_edit = await gateway.get_alias(edit_alias) if edit_alias else None
    alias_edit_id = edit_alias if alias_edit else None
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "gw": gw,
            "extra_headers_text": gateway.format_extra_headers(gw.get("extra_headers", {})),
            "has_key": bool(gw.get("api_key")),
            "saved": False,
            "error": None,
            "aliases": await gateway.list_aliases(),
            "alias_error": None,
            "alias_edit": alias_edit,
            "alias_edit_id": alias_edit_id,
            "alias_extra_headers_text": gateway.format_extra_headers(alias_edit["extra_headers"]) if alias_edit else "",
        },
    )


@app.post("/admin/settings", response_class=HTMLResponse)
async def settings_save(
    request: Request,
    name: str = Form(""),
    base_url: str = Form(...),
    api_key: str = Form(""),
    extra_headers: str = Form(""),
    default_model: str = Form(""),
):
    user = auth.require_admin(request)
    existing = await gateway.get_gateway_config() or {}
    error = None
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        error = "Base URL must start with http:// or https://"
    elif not api_key and not existing.get("api_key"):
        error = "API key is required (this gateway has no key stored yet)"

    if error:
        gw = {
            "name": name,
            "base_url": base_url,
            "extra_headers": gateway.parse_extra_headers(extra_headers),
            "default_model": default_model,
        }
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "user": user,
                "gw": gw,
                "extra_headers_text": extra_headers,
                "has_key": bool(existing.get("api_key")),
                "saved": False,
                "error": error,
                "aliases": await gateway.list_aliases(),
                "alias_error": None,
                "alias_edit": None,
                "alias_edit_id": None,
                "alias_extra_headers_text": "",
            },
            status_code=400,
        )

    await gateway.set_gateway_config(
        name=name,
        base_url=base_url,
        api_key=api_key,
        extra_headers=gateway.parse_extra_headers(extra_headers),
        default_model=default_model,
    )
    gw = await gateway.get_gateway_config()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "gw": gw,
            "extra_headers_text": gateway.format_extra_headers(gw.get("extra_headers", {})),
            "has_key": bool(gw.get("api_key")),
            "saved": True,
            "error": None,
            "aliases": await gateway.list_aliases(),
            "alias_error": None,
            "alias_edit": None,
            "alias_edit_id": None,
            "alias_extra_headers_text": "",
        },
    )


# --- Named model aliases (pinned targets for automation, e.g. Commander) --

@app.post("/admin/aliases", response_class=HTMLResponse)
async def alias_save(
    request: Request,
    alias: str = Form(...),
    name: str = Form(""),
    base_url: str = Form(...),
    api_key: str = Form(""),
    extra_headers: str = Form(""),
    default_model: str = Form(...),
):
    user = auth.require_admin(request)
    alias = alias.strip().lower()
    existing = await gateway.get_alias(alias) or {}
    error = None
    if not gateway.valid_alias(alias):
        error = "Alias must be lowercase letters/digits/hyphen/underscore, starting with a letter or digit."
    elif not (base_url.startswith("http://") or base_url.startswith("https://")):
        error = "Base URL must start with http:// or https://"
    elif not default_model.strip():
        error = "Default model is required for an alias -- it's always used verbatim, never admin-switchable."
    elif not api_key and not existing.get("api_key"):
        error = "API key is required (this alias has no key stored yet)"

    gw = await gateway.get_gateway_config() or dict(gateway.EMPTY_GATEWAY)
    if error:
        # Re-populate the form with what was just typed (not the stale
        # `existing` record) so a validation error doesn't lose the admin's
        # in-progress edit.
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "user": user,
                "gw": gw,
                "extra_headers_text": gateway.format_extra_headers(gw.get("extra_headers", {})),
                "has_key": bool(gw.get("api_key")),
                "saved": False,
                "error": None,
                "aliases": await gateway.list_aliases(),
                "alias_error": error,
                "alias_edit": {"name": name, "base_url": base_url, "default_model": default_model},
                "alias_edit_id": alias,
                "alias_extra_headers_text": extra_headers,
            },
            status_code=400,
        )

    await gateway.set_alias(
        alias,
        name=name,
        base_url=base_url,
        api_key=api_key,
        extra_headers=gateway.parse_extra_headers(extra_headers),
        default_model=default_model,
    )
    return RedirectResponse("/admin/settings", status_code=303)


@app.post("/admin/aliases/{alias}/delete")
async def alias_delete(request: Request, alias: str):
    auth.require_admin(request)
    await gateway.delete_alias(alias)
    return RedirectResponse("/admin/settings", status_code=303)
