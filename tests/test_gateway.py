import pytest

from app import gateway


def test_parse_extra_headers_basic():
    text = "cf-aig-gateway-id: my-gateway\nX-Foo: bar baz\n\n  \nno-colon-line"
    assert gateway.parse_extra_headers(text) == {"cf-aig-gateway-id": "my-gateway", "X-Foo": "bar baz"}


def test_parse_extra_headers_empty():
    assert gateway.parse_extra_headers("") == {}
    assert gateway.parse_extra_headers("   \n  \n") == {}


def test_format_extra_headers_roundtrip():
    headers = {"cf-aig-gateway-id": "my-gateway", "X-Foo": "bar"}
    text = gateway.format_extra_headers(headers)
    assert gateway.parse_extra_headers(text) == headers


async def test_get_gateway_config_none_when_unconfigured():
    assert await gateway.get_gateway_config() is None


async def test_get_gateway_config_bootstraps_from_cloudflare_env(monkeypatch):
    monkeypatch.setattr(gateway.config, "CF_ACCOUNT_ID", "acct123")
    monkeypatch.setattr(gateway.config, "CF_API_TOKEN", "tok123")
    monkeypatch.setattr(gateway.config, "CF_AIG_GATEWAY_ID", "my-gw")
    monkeypatch.setattr(gateway.config, "CF_MODEL", "@cf/qwen/qwen3.8-27b")

    gw = await gateway.get_gateway_config()
    assert gw["base_url"] == "https://api.cloudflare.com/client/v4/accounts/acct123/ai/v1/chat/completions"
    assert gw["api_key"] == "tok123"
    assert gw["extra_headers"] == {"cf-aig-gateway-id": "my-gw"}
    assert gw["default_model"] == "@cf/qwen/qwen3.8-27b"

    # Seeded into Redis -- a second call must return the same thing even if
    # the env vars disappear (Settings, once saved, is the source of truth).
    monkeypatch.setattr(gateway.config, "CF_API_TOKEN", None)
    gw2 = await gateway.get_gateway_config()
    assert gw2 == gw


async def test_get_gateway_config_bootstraps_from_generic_env(monkeypatch):
    monkeypatch.setattr(gateway.config, "CF_ACCOUNT_ID", None)
    monkeypatch.setattr(gateway.config, "CF_API_TOKEN", None)
    monkeypatch.setattr(gateway.config, "GATEWAY_NAME", "OpenAI")
    monkeypatch.setattr(gateway.config, "GATEWAY_BASE_URL", "https://api.openai.com/v1/chat/completions")
    monkeypatch.setattr(gateway.config, "GATEWAY_API_KEY", "sk-test")
    monkeypatch.setattr(gateway.config, "GATEWAY_EXTRA_HEADERS", None)
    monkeypatch.setattr(gateway.config, "GATEWAY_DEFAULT_MODEL", "gpt-4o-mini")

    gw = await gateway.get_gateway_config()
    assert gw["name"] == "OpenAI"
    assert gw["base_url"] == "https://api.openai.com/v1/chat/completions"
    assert gw["api_key"] == "sk-test"
    assert gw["default_model"] == "gpt-4o-mini"


async def test_set_gateway_config_blank_api_key_keeps_existing():
    await gateway.set_gateway_config(
        name="OpenAI", base_url="https://api.openai.com/v1/chat/completions",
        api_key="sk-original", extra_headers={}, default_model="gpt-4o-mini",
    )
    # Re-save with a blank api_key (simulates re-submitting the Settings
    # form without retyping the secret) -- must not wipe it out.
    await gateway.set_gateway_config(
        name="OpenAI", base_url="https://api.openai.com/v1/chat/completions",
        api_key="", extra_headers={"X-Foo": "bar"}, default_model="gpt-4o-mini",
    )
    gw = await gateway.get_gateway_config()
    assert gw["api_key"] == "sk-original"
    assert gw["extra_headers"] == {"X-Foo": "bar"}


async def test_set_gateway_config_new_api_key_replaces_it():
    await gateway.set_gateway_config(
        name="OpenAI", base_url="https://api.openai.com/v1/chat/completions",
        api_key="sk-original", extra_headers={}, default_model="",
    )
    await gateway.set_gateway_config(
        name="OpenAI", base_url="https://api.openai.com/v1/chat/completions",
        api_key="sk-rotated", extra_headers={}, default_model="",
    )
    gw = await gateway.get_gateway_config()
    assert gw["api_key"] == "sk-rotated"


def test_request_headers_merges_auth_and_extra():
    gw = {"api_key": "sk-abc", "extra_headers": {"cf-aig-gateway-id": "gw1"}}
    headers = gateway.request_headers(gw)
    assert headers["Authorization"] == "Bearer sk-abc"
    assert headers["cf-aig-gateway-id"] == "gw1"
    assert headers["Content-Type"] == "application/json"


# --- Named aliases ----------------------------------------------------

def test_valid_alias():
    assert gateway.valid_alias("qwen-fast")
    assert gateway.valid_alias("gemini_pro")
    assert gateway.valid_alias("a")
    assert not gateway.valid_alias("")
    assert not gateway.valid_alias("Qwen-Fast")  # uppercase not allowed
    assert not gateway.valid_alias("-leading-hyphen")
    assert not gateway.valid_alias("has a space")


async def test_get_alias_none_when_unset():
    assert await gateway.get_alias("qwen-fast") is None


async def test_set_and_get_alias_roundtrip():
    await gateway.set_alias(
        "qwen-fast", name="Qwen", base_url="https://example.com/v1/chat/completions",
        api_key="sk-qwen", extra_headers={"cf-aig-gateway-id": "gw1"}, default_model="@cf/qwen/qwen3.8-27b",
    )
    gw = await gateway.get_alias("qwen-fast")
    assert gw["name"] == "Qwen"
    assert gw["api_key"] == "sk-qwen"
    assert gw["default_model"] == "@cf/qwen/qwen3.8-27b"


async def test_set_alias_blank_api_key_keeps_existing():
    await gateway.set_alias(
        "gemini-pro", name="Gemini", base_url="https://example.com/v1", api_key="sk-original",
        extra_headers={}, default_model="gemini-2.5-pro",
    )
    await gateway.set_alias(
        "gemini-pro", name="Gemini", base_url="https://example.com/v1", api_key="",
        extra_headers={}, default_model="gemini-2.5-pro",
    )
    gw = await gateway.get_alias("gemini-pro")
    assert gw["api_key"] == "sk-original"


async def test_list_aliases_returns_all_saved():
    await gateway.set_alias(
        "qwen-fast", name="Qwen", base_url="https://a", api_key="k1", extra_headers={}, default_model="m1",
    )
    await gateway.set_alias(
        "gemini-pro", name="Gemini", base_url="https://b", api_key="k2", extra_headers={}, default_model="m2",
    )
    aliases = await gateway.list_aliases()
    assert set(aliases.keys()) == {"qwen-fast", "gemini-pro"}


async def test_delete_alias_removes_it():
    await gateway.set_alias(
        "qwen-fast", name="Qwen", base_url="https://a", api_key="k1", extra_headers={}, default_model="m1",
    )
    await gateway.delete_alias("qwen-fast")
    assert await gateway.get_alias("qwen-fast") is None
