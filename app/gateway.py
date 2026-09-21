"""Generic OpenAI-compatible upstream config -- Cloudflare AI Gateway,
OpenAI, OpenRouter, Groq, a local Ollama, or anything else that speaks the
`/v1/chat/completions` shape. Not tied to Cloudflare: that was this app's
original (and still default) backend, but any provider works as long as it
accepts `Authorization: Bearer <api_key>` plus optional extra headers.

Config lives in Redis (one JSON hash) so both replicas see edits made from
/admin/settings immediately, no redeploy needed -- same pattern already used
for the "active model" setting. On first boot with nothing in Redis yet, it
is seeded once from env vars (see config.py) so existing env-var-only
deployments keep working unchanged after upgrading to this version.
"""
import json
import re

from . import config, redis_client

GATEWAY_KEY = "gateway_config"

EMPTY_GATEWAY = {
    "name": "",
    "base_url": "",
    "api_key": "",
    "extra_headers": {},
    "default_model": "",
}


def _bootstrap_from_env() -> dict | None:
    if config.CF_ACCOUNT_ID and config.CF_API_TOKEN:
        headers = {}
        if config.CF_AIG_GATEWAY_ID:
            headers["cf-aig-gateway-id"] = config.CF_AIG_GATEWAY_ID
        return {
            "name": "Cloudflare Workers AI",
            "base_url": (
                f"https://api.cloudflare.com/client/v4/accounts/{config.CF_ACCOUNT_ID}"
                "/ai/v1/chat/completions"
            ),
            "api_key": config.CF_API_TOKEN,
            "extra_headers": headers,
            "default_model": config.CF_MODEL or "",
        }
    if config.GATEWAY_BASE_URL and config.GATEWAY_API_KEY:
        return {
            "name": config.GATEWAY_NAME or "Custom gateway",
            "base_url": config.GATEWAY_BASE_URL,
            "api_key": config.GATEWAY_API_KEY,
            "extra_headers": parse_extra_headers(config.GATEWAY_EXTRA_HEADERS or ""),
            "default_model": config.GATEWAY_DEFAULT_MODEL or "",
        }
    return None


def parse_extra_headers(text: str) -> dict:
    """Parses the Settings form's textarea, one `Header-Name: value` per
    line. Blank lines and lines without a colon are silently skipped rather
    than rejected -- this is a convenience field, not a strict format."""
    headers = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if name:
            headers[name] = value
    return headers


def format_extra_headers(headers: dict) -> str:
    return "\n".join(f"{k}: {v}" for k, v in headers.items())


async def get_gateway_config() -> dict | None:
    """Returns the current gateway config, or None if nothing is configured
    yet (fresh install, no env bootstrap, nothing saved via Settings)."""
    raw = await redis_client.get_raw(GATEWAY_KEY)
    if raw:
        return json.loads(raw)
    seed = _bootstrap_from_env()
    if seed:
        await redis_client.set_raw(GATEWAY_KEY, json.dumps(seed))
        return seed
    return None


async def set_gateway_config(
    *, name: str, base_url: str, api_key: str | None, extra_headers: dict, default_model: str
) -> None:
    """`api_key=None` (or blank) keeps whatever key is already stored --
    lets the Settings form be re-saved (e.g. to fix a header) without
    forcing the admin to re-paste a secret they can't see again."""
    current = await get_gateway_config() or dict(EMPTY_GATEWAY)
    record = {
        "name": name.strip(),
        "base_url": base_url.strip().rstrip("/"),
        "api_key": api_key.strip() if api_key and api_key.strip() else current.get("api_key", ""),
        "extra_headers": extra_headers,
        "default_model": default_model.strip(),
    }
    await redis_client.set_raw(GATEWAY_KEY, json.dumps(record))


# --- Named aliases ------------------------------------------------------
# A second, independent tier of gateway configs on top of the single
# "current" one above: each alias is a *pinned* provider+model, selected by
# a client naming it directly in the request's `model` field (e.g.
# Commander's delegate_task("qwen-fast", ...)). Unlike the default gateway,
# an alias is never affected by the admin's dashboard "active model"
# switch -- it always uses its own stored `default_model` verbatim, so it
# stays a stable target for automation even while an operator is
# interactively flipping the default gateway's model around.
ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def valid_alias(alias: str) -> bool:
    return bool(ALIAS_RE.match(alias))


async def list_aliases() -> dict:
    raw = await redis_client.gateways_hgetall()
    return {alias: json.loads(v) for alias, v in raw.items()}


async def get_alias(alias: str) -> dict | None:
    raw = await redis_client.gateways_hget(alias)
    return json.loads(raw) if raw else None


async def set_alias(
    alias: str, *, name: str, base_url: str, api_key: str | None, extra_headers: dict, default_model: str
) -> None:
    """Same `api_key=None`-keeps-existing behavior as set_gateway_config."""
    current = await get_alias(alias) or dict(EMPTY_GATEWAY)
    record = {
        "name": name.strip(),
        "base_url": base_url.strip().rstrip("/"),
        "api_key": api_key.strip() if api_key and api_key.strip() else current.get("api_key", ""),
        "extra_headers": extra_headers,
        "default_model": default_model.strip(),
    }
    await redis_client.gateways_hset(alias, json.dumps(record))


async def delete_alias(alias: str) -> None:
    await redis_client.gateways_hdel(alias)


def request_headers(gw: dict) -> dict:
    return {
        "Authorization": f"Bearer {gw['api_key']}",
        "Content-Type": "application/json",
        **gw.get("extra_headers", {}),
    }
