from fastapi import APIRouter

from . import gateway, redis_client

router = APIRouter()


@router.get("/status")
async def status():
    # Fast path for a dashboard poller (light-up indicator, ~5s
    # active-viewer cadence): reads Redis only, never calls out to the
    # upstream gateway itself, so polling this costs nothing against a paid
    # provider's quota no matter how often it's hit. gateway.get_gateway_config()
    # is a Redis read too (with a one-time env-var seed on first call), not
    # an outbound request.
    gw = await gateway.get_gateway_config()
    return await redis_client.get_status(gw["default_model"] if gw else "")


@router.get("/healthz")
async def healthz():
    # Deliberately doesn't depend on Redis or the upstream gateway -- a
    # Redis blip or a slow self-test shouldn't make k8s kill/recycle this pod.
    return {"ok": True}
