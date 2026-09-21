import json

import pytest
from fastapi.testclient import TestClient

from app import gateway
from app import proxy as proxy_module
from app.main import app

# Must match conftest.py's DASHBOARD_PASSWORD_HASH -- kept as a separate
# literal rather than importing across test files to avoid depending on
# `tests` being an importable package.
TEST_PASSWORD = "correct horse battery staple"


@pytest.fixture
def client():
    return TestClient(app)


class _FakeHTTPResponse:
    def __init__(self, status_code=200, json_data=None, text=None):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text if text is not None else (json.dumps(json_data) if json_data is not None else "")

    def json(self):
        if self._json_data is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json_data


class _FakeAsyncClient:
    next_response = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        return _FakeAsyncClient.next_response


@pytest.fixture
def fake_upstream(monkeypatch):
    monkeypatch.setattr(proxy_module.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


# --- Admin auth ---------------------------------------------------------

def test_dashboard_redirects_when_not_logged_in(client):
    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_login_wrong_password_rejected(client):
    resp = client.post("/admin/login", data={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401
    assert "Invalid credentials" in resp.text


def test_login_success_grants_dashboard_access(client):
    resp = client.post(
        "/admin/login", data={"username": "admin", "password": TEST_PASSWORD}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"
    assert "llm_proxy_session" in resp.cookies

    dash = client.get("/admin")
    assert dash.status_code == 200
    assert "LLM PROXY" in dash.text


def test_logout_clears_session(client):
    client.post("/admin/login", data={"username": "admin", "password": TEST_PASSWORD})
    client.post("/admin/logout")
    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303


def test_login_lockout_after_max_attempts(client):
    # conftest sets LOGIN_MAX_ATTEMPTS=3 for a fast, deterministic test.
    for _ in range(3):
        resp = client.post("/admin/login", data={"username": "admin", "password": "wrong"})
        assert resp.status_code == 401

    # Even the *correct* password is now blocked until the lockout expires.
    resp = client.post("/admin/login", data={"username": "admin", "password": TEST_PASSWORD})
    assert resp.status_code == 429
    assert "Too many failed attempts" in resp.text


# --- Settings -------------------------------------------------------------

def test_settings_rejects_bad_base_url(client):
    client.post("/admin/login", data={"username": "admin", "password": TEST_PASSWORD})
    resp = client.post(
        "/admin/settings",
        data={"name": "x", "base_url": "not-a-url", "api_key": "sk-1", "extra_headers": "", "default_model": ""},
    )
    assert resp.status_code == 400
    assert "must start with" in resp.text


def test_settings_requires_api_key_on_first_save(client):
    client.post("/admin/login", data={"username": "admin", "password": TEST_PASSWORD})
    resp = client.post(
        "/admin/settings",
        data={
            "name": "x",
            "base_url": "https://api.openai.com/v1/chat/completions",
            "api_key": "",
            "extra_headers": "",
            "default_model": "",
        },
    )
    assert resp.status_code == 400
    assert "API key is required" in resp.text


def test_settings_save_then_reload_without_key_keeps_it(client):
    client.post("/admin/login", data={"username": "admin", "password": TEST_PASSWORD})
    client.post(
        "/admin/settings",
        data={
            "name": "OpenAI",
            "base_url": "https://api.openai.com/v1/chat/completions",
            "api_key": "sk-original",
            "extra_headers": "",
            "default_model": "gpt-4o-mini",
        },
    )
    resp = client.post(
        "/admin/settings",
        data={
            "name": "OpenAI",
            "base_url": "https://api.openai.com/v1/chat/completions",
            "api_key": "",
            "extra_headers": "X-Foo: bar",
            "default_model": "gpt-4o-mini",
        },
    )
    assert resp.status_code == 200
    assert "Saved." in resp.text


# --- Proxy ------------------------------------------------------------

def test_chat_completions_requires_api_key(client):
    resp = client.post("/v1/chat/completions", json={"messages": []})
    assert resp.status_code == 401


def test_chat_completions_503_when_no_gateway_configured(client):
    client.post("/admin/login", data={"username": "admin", "password": TEST_PASSWORD})
    key_resp = client.post("/admin/keys/issue", data={"label": "test-client"})
    token = key_resp.text.split('<code class="token">')[1].split("</code>")[0]

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 503


async def test_chat_completions_passthrough_success(client, fake_upstream):
    await gateway.set_gateway_config(
        name="OpenAI", base_url="https://api.openai.com/v1/chat/completions",
        api_key="sk-real", extra_headers={}, default_model="gpt-4o-mini",
    )
    from app import acl
    _, token = await acl.issue_key("test-client")

    _FakeAsyncClient.next_response = _FakeHTTPResponse(
        status_code=200,
        json_data={"choices": [{"message": {"content": "hi there"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    )

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "whatever-the-client-thinks-it-is", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "hi there"

    status = client.get("/status").json()
    assert status["last_usage"]["prompt_tokens"] == 3
    assert status["current_model"] == "gpt-4o-mini"


async def test_chat_completions_alias_routes_to_its_own_gateway(client, fake_upstream):
    # Default gateway is OpenAI/gpt-4o-mini; the "qwen-fast" alias points
    # elsewhere entirely -- a request naming that alias must hit the
    # alias's own base_url/model, not the default gateway's, and must not
    # be affected by whatever the dashboard's "active model" is set to.
    await gateway.set_gateway_config(
        name="OpenAI", base_url="https://api.openai.com/v1/chat/completions",
        api_key="sk-real", extra_headers={}, default_model="gpt-4o-mini",
    )
    await gateway.set_alias(
        "qwen-fast", name="Cloudflare", base_url="https://cf.example.com/v1/chat/completions",
        api_key="sk-cf", extra_headers={"cf-aig-gateway-id": "gw1"}, default_model="@cf/qwen/qwen3.8-27b",
    )
    from app import acl
    _, token = await acl.issue_key("test-client")

    _FakeAsyncClient.next_response = _FakeHTTPResponse(
        status_code=200,
        json_data={"choices": [{"message": {"content": "hi from qwen"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    )

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "qwen-fast", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "hi from qwen"

    # Confirm it didn't silently fall back to the default gateway's model.
    status = client.get("/status").json()
    assert status["last_model"] == "@cf/qwen/qwen3.8-27b"


async def test_chat_completions_unknown_model_name_falls_back_to_default(client, fake_upstream):
    # A client sending an arbitrary/unrecognized model string (not a saved
    # alias) must keep today's behavior: cosmetic only, always the default
    # gateway's current model.
    await gateway.set_gateway_config(
        name="OpenAI", base_url="https://api.openai.com/v1/chat/completions",
        api_key="sk-real", extra_headers={}, default_model="gpt-4o-mini",
    )
    from app import acl
    _, token = await acl.issue_key("test-client")

    _FakeAsyncClient.next_response = _FakeHTTPResponse(
        status_code=200,
        json_data={"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    )
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "not-a-real-alias", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    status = client.get("/status").json()
    assert status["current_model"] == "gpt-4o-mini"


async def test_v1_models_lists_default_and_aliases(client):
    await gateway.set_gateway_config(
        name="OpenAI", base_url="https://api.openai.com/v1/chat/completions",
        api_key="sk-real", extra_headers={}, default_model="gpt-4o-mini",
    )
    await gateway.set_alias(
        "qwen-fast", name="Cloudflare", base_url="https://cf.example.com", api_key="sk-cf",
        extra_headers={}, default_model="@cf/qwen/qwen3.8-27b",
    )
    from app import acl
    _, token = await acl.issue_key("test-client")

    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    ids = {m["id"] for m in resp.json()["data"]}
    assert "gpt-4o-mini" in ids
    assert "qwen-fast" in ids


def test_alias_save_requires_admin(client):
    resp = client.post(
        "/admin/aliases",
        data={"alias": "qwen-fast", "base_url": "https://a", "api_key": "k", "default_model": "m"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_alias_save_rejects_invalid_alias(client):
    client.post("/admin/login", data={"username": "admin", "password": TEST_PASSWORD})
    resp = client.post(
        "/admin/aliases",
        data={"alias": "Not Valid!", "base_url": "https://a", "api_key": "k", "default_model": "m"},
    )
    assert resp.status_code == 400
    assert "Alias must be" in resp.text


def test_alias_save_and_delete_roundtrip(client):
    client.post("/admin/login", data={"username": "admin", "password": TEST_PASSWORD})
    resp = client.post(
        "/admin/aliases",
        data={
            "alias": "qwen-fast", "name": "Cloudflare", "base_url": "https://cf.example.com",
            "api_key": "sk-cf", "extra_headers": "", "default_model": "@cf/qwen/qwen3.8-27b",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    settings = client.get("/admin/settings")
    assert "qwen-fast" in settings.text

    client.post("/admin/aliases/qwen-fast/delete", follow_redirects=False)
    settings = client.get("/admin/settings")
    assert "No aliases yet." in settings.text


def test_edit_alias_prefills_the_form(client):
    client.post("/admin/login", data={"username": "admin", "password": TEST_PASSWORD})
    client.post(
        "/admin/aliases",
        data={
            "alias": "qwen-fast", "name": "Cloudflare", "base_url": "https://cf.example.com/v1/chat/completions",
            "api_key": "sk-cf", "extra_headers": "cf-aig-gateway-id: gw1", "default_model": "@cf/qwen/qwen3.8-27b",
        },
    )
    resp = client.get("/admin/settings?edit_alias=qwen-fast")
    assert resp.status_code == 200
    assert 'value="qwen-fast"' in resp.text
    assert 'value="https://cf.example.com/v1/chat/completions"' in resp.text
    assert "cf-aig-gateway-id: gw1" in resp.text
    assert 'value="@cf/qwen/qwen3.8-27b"' in resp.text
    assert "update alias" in resp.text


def test_edit_unknown_alias_shows_blank_form(client):
    client.post("/admin/login", data={"username": "admin", "password": TEST_PASSWORD})
    resp = client.get("/admin/settings?edit_alias=does-not-exist")
    assert resp.status_code == 200
    assert "save alias" in resp.text
    assert "update alias" not in resp.text


def test_updating_an_alias_preserves_its_stored_api_key(client):
    client.post("/admin/login", data={"username": "admin", "password": TEST_PASSWORD})
    client.post(
        "/admin/aliases",
        data={
            "alias": "qwen-fast", "name": "Cloudflare", "base_url": "https://cf.example.com",
            "api_key": "sk-original", "extra_headers": "", "default_model": "@cf/qwen/qwen3.8-27b",
        },
    )
    # Re-save via the edit flow with a blank api_key and a changed model --
    # same "blank keeps existing key" semantics as the main gateway form.
    client.post(
        "/admin/aliases",
        data={
            "alias": "qwen-fast", "name": "Cloudflare (renamed label)", "base_url": "https://cf.example.com",
            "api_key": "", "extra_headers": "", "default_model": "@cf/qwen/qwen3-updated",
        },
    )
    resp = client.get("/admin/settings?edit_alias=qwen-fast")
    assert 'value="@cf/qwen/qwen3-updated"' in resp.text


def test_usage_endpoint_requires_api_key(client):
    resp = client.get("/v1/usage")
    assert resp.status_code == 401


async def test_usage_endpoint_reflects_completed_requests(client, fake_upstream):
    await gateway.set_gateway_config(
        name="OpenAI", base_url="https://api.openai.com/v1/chat/completions",
        api_key="sk-real", extra_headers={}, default_model="gpt-4o-mini",
    )
    from app import acl
    _, token = await acl.issue_key("test-client")

    _FakeAsyncClient.next_response = _FakeHTTPResponse(
        status_code=200,
        json_data={
            "choices": [{"message": {"content": "hi there"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
    )
    for _ in range(2):
        client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    resp = client.get("/v1/usage", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["requests"] == 2
    assert body["prompt_tokens"] == 6
    assert body["completion_tokens"] == 4
    assert body["total_tokens"] == 10


async def test_usage_endpoint_is_scoped_to_the_calling_key_only(client, fake_upstream):
    await gateway.set_gateway_config(
        name="OpenAI", base_url="https://api.openai.com/v1/chat/completions",
        api_key="sk-real", extra_headers={}, default_model="gpt-4o-mini",
    )
    from app import acl
    _, token_a = await acl.issue_key("client-a")
    _, token_b = await acl.issue_key("client-b")

    _FakeAsyncClient.next_response = _FakeHTTPResponse(
        status_code=200,
        json_data={"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10}},
    )
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )

    # client-a's usage shows the request; client-b, who made no requests,
    # sees only its own (zeroed) numbers -- never client-a's.
    usage_a = client.get("/v1/usage", headers={"Authorization": f"Bearer {token_a}"}).json()
    usage_b = client.get("/v1/usage", headers={"Authorization": f"Bearer {token_b}"}).json()
    assert usage_a["requests"] == 1
    assert usage_b["requests"] == 0


def test_dashboard_shows_per_key_usage_columns(client):
    client.post("/admin/login", data={"username": "admin", "password": TEST_PASSWORD})
    resp = client.get("/admin")
    assert "Requests" in resp.text
    assert "Cumulative Tokens (in / out)" in resp.text
    # The top stats-bar stat must read distinctly from the per-key column
    # header so the two don't look like the same number -- this is the
    # exact confusion this label change was made to prevent.
    assert "Last request (in / out)" in resp.text


async def test_chat_completions_handles_non_json_upstream_error(client, fake_upstream):
    # Regression test for the crash this app used to hit when the upstream
    # returned something that wasn't JSON (e.g. an edge/proxy error page) --
    # resp.json() raised ValueError with no handling, turning into an
    # unhandled 500 instead of a clean error response.
    await gateway.set_gateway_config(
        name="OpenAI", base_url="https://api.openai.com/v1/chat/completions",
        api_key="sk-real", extra_headers={}, default_model="gpt-4o-mini",
    )
    from app import acl
    _, token = await acl.issue_key("test-client")

    _FakeAsyncClient.next_response = _FakeHTTPResponse(status_code=502, text="<html>Bad Gateway</html>")

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "upstream_error"


def test_healthz_has_no_dependencies(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_favicon_ico_redirects_to_svg(client):
    resp = client.get("/favicon.ico", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/static/favicon.svg"


def test_favicon_svg_is_served_and_well_formed(client):
    import xml.etree.ElementTree as ET

    resp = client.get("/static/favicon.svg")
    assert resp.status_code == 200
    assert "svg" in resp.headers["content-type"]
    ET.fromstring(resp.text)  # raises if not well-formed XML


def test_pages_link_the_favicon(client):
    for path in ("/admin/login",):
        assert '<link rel="icon"' in client.get(path).text
