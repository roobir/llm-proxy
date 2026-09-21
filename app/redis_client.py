import json
import time

import redis.asyncio as redis

from . import config

_pool = redis.from_url(config.REDIS_URL, decode_responses=True)

_STATUS_KEYS = [
    "inflight",
    "last_active_ts",
    "last_model",
    "last_usage",
    "selftest:status",
    "selftest:ts",
    "selftest:detail",
]


async def incr_inflight() -> None:
    async with _pool.pipeline() as p:
        p.incr("inflight")
        p.set("last_active_ts", time.time())
        await p.execute()


async def decr_inflight() -> None:
    val = await _pool.decr("inflight")
    if val < 0:
        # Defensive only -- shouldn't happen since every incr has a
        # matching decr in a finally block, but a negative count would
        # otherwise permanently show "processing" as false when it isn't.
        await _pool.set("inflight", 0)
    await _pool.set("last_active_ts", time.time())


async def set_last_usage(model: str, usage: dict) -> None:
    async with _pool.pipeline() as p:
        p.set("last_model", model)
        p.set("last_usage", json.dumps(usage))
        p.set("last_active_ts", time.time())
        await p.execute()


async def get_current_model(gateway_default: str = "") -> str:
    # The admin-selected target model for new requests -- distinct from
    # last_model below (what the *last actual request* used, which could
    # briefly lag this if changed mid-flight). Falls back to the active
    # gateway's default_model until an admin has ever set one via the GUI.
    val = await _pool.get("current_model")
    return val or gateway_default


async def set_current_model(model: str) -> None:
    await _pool.set("current_model", model)


async def get_raw(key: str) -> str | None:
    return await _pool.get(key)


async def set_raw(key: str, value: str) -> None:
    await _pool.set(key, value)


# --- Named gateway aliases (e.g. "qwen-fast", "gemini-pro") ---------------
# One Redis hash, field = alias, value = JSON gateway record (same shape as
# the single gateway_config record) -- lets a client pin a request to a
# specific provider/model by name, separate from the one admin-switchable
# "current model" the default/unnamed path still uses.

async def gateways_hgetall() -> dict:
    return await _pool.hgetall("gateways")


async def gateways_hget(alias: str) -> str | None:
    return await _pool.hget("gateways", alias)


async def gateways_hset(alias: str, value: str) -> None:
    await _pool.hset("gateways", alias, value)


async def gateways_hdel(alias: str) -> None:
    await _pool.hdel("gateways", alias)


async def get_status(gateway_default_model: str = "") -> dict:
    vals = await _pool.mget(_STATUS_KEYS)
    d = dict(zip(_STATUS_KEYS, vals))
    inflight = int(d["inflight"] or 0)
    return {
        "inflight": inflight,
        "state": "PROCESSING" if inflight > 0 else "IDLE",
        "last_active_ts": float(d["last_active_ts"]) if d["last_active_ts"] else None,
        "current_model": await get_current_model(gateway_default_model),
        "last_model": d["last_model"] or None,
        "last_usage": json.loads(d["last_usage"]) if d["last_usage"] else None,
        "gateway": {
            "status": d["selftest:status"] or "unknown",
            "last_checked": float(d["selftest:ts"]) if d["selftest:ts"] else None,
            "detail": d["selftest:detail"] or "",
        },
    }


async def set_selftest_result(ok: bool, detail: str = "") -> None:
    async with _pool.pipeline() as p:
        p.set("selftest:status", "ok" if ok else "fail")
        p.set("selftest:ts", time.time())
        p.set("selftest:detail", detail)
        await p.execute()


# --- ACL (per-client API keys) ---------------------------------------------
# One Redis hash, field = key_id, value = JSON record. Small enough (a
# handful of clients) that a full HGETALL per validate() call is cheap --
# no reason to build a smarter index for this scale.

async def acl_list() -> dict:
    raw = await _pool.hgetall("acl")
    return {k: json.loads(v) for k, v in raw.items()}


async def acl_get(key_id: str) -> dict | None:
    raw = await _pool.hget("acl", key_id)
    return json.loads(raw) if raw else None


async def acl_put(key_id: str, record: dict) -> None:
    await _pool.hset("acl", key_id, json.dumps(record))


async def acl_delete(key_id: str) -> None:
    await _pool.hdel("acl", key_id)
    await _pool.delete(f"usage:{key_id}")


# --- Per-key usage (cumulative, across every request that key has ever
# made) ---------------------------------------------------------------------
# One Redis hash per key_id -- HINCRBY makes concurrent requests from the
# same client (both replicas, multiple in-flight requests) safe to tally
# without a read-modify-write race, same reasoning as the inflight counter.

_USAGE_FIELDS = ["requests", "prompt_tokens", "completion_tokens", "total_tokens"]


async def incr_key_usage(key_id: str, usage: dict) -> None:
    async with _pool.pipeline() as p:
        p.hincrby(f"usage:{key_id}", "requests", 1)
        p.hincrby(f"usage:{key_id}", "prompt_tokens", int(usage.get("prompt_tokens") or 0))
        p.hincrby(f"usage:{key_id}", "completion_tokens", int(usage.get("completion_tokens") or 0))
        p.hincrby(f"usage:{key_id}", "total_tokens", int(usage.get("total_tokens") or 0))
        await p.execute()


async def get_key_usage(key_id: str) -> dict:
    raw = await _pool.hgetall(f"usage:{key_id}")
    return {field: int(raw.get(field) or 0) for field in _USAGE_FIELDS}


async def get_all_key_usage(key_ids: list[str]) -> dict[str, dict]:
    if not key_ids:
        return {}
    async with _pool.pipeline() as p:
        for key_id in key_ids:
            p.hgetall(f"usage:{key_id}")
        results = await p.execute()
    return {
        key_id: {field: int(raw.get(field) or 0) for field in _USAGE_FIELDS}
        for key_id, raw in zip(key_ids, results)
    }


# --- Login lockout (per source IP) -----------------------------------------
# A plain counter with a TTL -- not trying to be a real WAF, just enough to
# stop naive password-guessing bots from hammering the one admin account
# this app has. Resets itself (TTL expires) rather than needing an unlock
# action from anywhere.

async def login_failure_count(ip: str) -> int:
    return int(await _pool.get(f"login_fail:{ip}") or 0)


async def register_login_failure(ip: str, lockout_seconds: int) -> int:
    key = f"login_fail:{ip}"
    async with _pool.pipeline() as p:
        p.incr(key)
        p.expire(key, lockout_seconds)
        count, _ = await p.execute()
    return count


async def clear_login_failures(ip: str) -> None:
    await _pool.delete(f"login_fail:{ip}")
