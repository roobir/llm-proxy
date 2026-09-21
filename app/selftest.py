import asyncio
import sys

import httpx

from . import config, gateway, redis_client


async def run_selftest() -> tuple[bool, str]:
    """Round-trips a trivial real request through the actual outbound path
    (relay -> configured AI gateway -> back) to prove the one dependency
    this whole app rests on is actually reachable. Deliberately not called
    from the fast /status path -- each call here is a real, logged/billed
    request against whatever provider is configured."""
    gw = await gateway.get_gateway_config()
    if not gw:
        return False, "no AI gateway configured -- set one up in /admin/settings"
    body = {
        "model": await redis_client.get_current_model(gw["default_model"]),
        "messages": [{"role": "user", "content": config.SELFTEST_PROMPT}],
        "max_tokens": 8,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(gw["base_url"], headers=gateway.request_headers(gw), json=body)
        try:
            ok = resp.status_code == 200 and bool(resp.json().get("choices"))
        except ValueError:
            ok = False
        if ok:
            return True, ""
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    except Exception as exc:  # noqa: BLE001 -- any failure here just means "gateway down"
        return False, str(exc)


async def _main() -> None:
    # Entrypoint for the k3s CronJob (`python -m app.selftest`) -- runs
    # exactly once per invocation regardless of how many app replicas exist,
    # so 2 replicas can never double-fire the same billed self-test.
    ok, detail = await run_selftest()
    await redis_client.set_selftest_result(ok, detail)
    print(f"selftest {'OK' if ok else 'FAILED'}: {detail}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(_main())
