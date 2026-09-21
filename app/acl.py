import hashlib
import hmac
import secrets
import time
from datetime import datetime, timezone

from . import redis_client


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_key(label: str) -> tuple[str, str]:
    """Returns (key_id, raw_token). raw_token is shown exactly once -- only
    its hash is ever stored, same as most AI gateway providers' own
    token-issuing UX (e.g. Cloudflare AI Gateway)."""
    key_id = secrets.token_hex(4)
    raw_token = f"llm_{key_id}_{secrets.token_urlsafe(24)}"
    record = {
        "label": label,
        "token_hash": _hash(raw_token),
        "created_at": time.time(),
        "revoked": False,
    }
    await redis_client.acl_put(key_id, record)
    return key_id, raw_token


async def revoke_key(key_id: str) -> None:
    record = await redis_client.acl_get(key_id)
    if record:
        record["revoked"] = True
        await redis_client.acl_put(key_id, record)


async def delete_key(key_id: str) -> None:
    await redis_client.acl_delete(key_id)


async def validate(raw_token: str) -> str | None:
    """Returns the client's key_id if raw_token is a valid, non-revoked key."""
    if not raw_token:
        return None
    token_hash = _hash(raw_token)
    for key_id, record in (await redis_client.acl_list()).items():
        if hmac.compare_digest(record["token_hash"], token_hash) and not record.get("revoked"):
            return key_id
    return None


async def record_usage(key_id: str, usage: dict) -> None:
    """Tallies one request's token usage onto this key's running total --
    called once per completed request (streaming or not), same moment the
    old app-wide-only stats were recorded. A revoked key still accrues
    usage if it somehow gets used (shouldn't happen -- validate() already
    rejects revoked keys before a request reaches this point), so this is
    purely additive bookkeeping, never a gate."""
    await redis_client.incr_key_usage(key_id, usage)


async def get_usage(key_id: str) -> dict:
    """A single key's own cumulative usage -- used by the self-service
    GET /v1/usage endpoint, where a client can only ever ask for its own
    key's numbers (key_id comes from validate()'s own token lookup, never
    a client-supplied id), so there's no cross-client enumeration risk."""
    return await redis_client.get_key_usage(key_id)


async def list_keys() -> list[dict]:
    acl = await redis_client.acl_list()
    usage = await redis_client.get_all_key_usage(list(acl.keys()))
    return [
        {
            "id": k,
            "label": v["label"],
            "created_at": datetime.fromtimestamp(v["created_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "revoked": v.get("revoked", False),
            "usage": usage.get(k, {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
        }
        for k, v in sorted(acl.items(), key=lambda kv: kv[1]["created_at"], reverse=True)
    ]
