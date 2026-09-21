import json

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import acl, gateway, redis_client

router = APIRouter()


async def _require_client(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    client_id = await acl.validate(token)
    if not client_id:
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    return client_id


async def _require_gateway() -> dict:
    gw = await gateway.get_gateway_config()
    if not gw:
        # No provider configured yet (fresh install, nothing in Settings,
        # no bootstrap env vars) -- a clear 503 beats a confusing crash deep
        # inside httpx for a client hitting this before the admin has set
        # anything up.
        raise HTTPException(
            status_code=503,
            detail="no AI gateway configured -- sign in to /admin/settings and configure one",
        )
    return gw


async def _resolve_target(requested_model: str) -> tuple[dict, str]:
    """Picks which gateway+model this request actually hits. If the
    client's `model` field names a saved alias, that alias's own gateway
    config and pinned `default_model` are used verbatim -- deliberately
    bypassing the admin's dashboard "active model" switch, since an alias
    (e.g. Commander's "qwen-fast"/"gemini-pro") exists to be a stable
    automation target, not something that should silently move when an
    operator flips the default model around interactively. Anything else
    (blank, or a name that isn't a known alias) keeps the original
    single-gateway behavior unchanged, including the free-text client
    model name being treated as cosmetic only."""
    if requested_model:
        alias_gw = await gateway.get_alias(requested_model)
        if alias_gw:
            return alias_gw, alias_gw["default_model"]
    gw = await _require_gateway()
    current_model = await redis_client.get_current_model(gw["default_model"])
    return gw, current_model


@router.get("/v1/models")
async def list_models(request: Request):
    # Lets clients that do a model-discovery call first (OpenCode included)
    # keep working unchanged -- same response shape a local llama.cpp server
    # would return, describing the one real "default" model currently
    # active, plus one entry per named alias so a caller can discover what
    # it's allowed to ask for by name without hardcoding it.
    await _require_client(request)
    data = []
    gw = await gateway.get_gateway_config()
    if gw:
        current_model = await redis_client.get_current_model(gw["default_model"])
        data.append({"id": current_model, "object": "model", "owned_by": gw["name"] or "llm-proxy"})
    for alias, cfg in (await gateway.list_aliases()).items():
        data.append({"id": alias, "object": "model", "owned_by": cfg.get("name") or "llm-proxy"})
    return JSONResponse({"object": "list", "data": data})


@router.get("/v1/usage")
async def usage(request: Request):
    # Self-service: a key can only ever see its own numbers, since key_id
    # comes from validate()'s own lookup of the caller's bearer token, never
    # a client-supplied id -- no path/query param means no way to ask for
    # anyone else's usage.
    client_id = await _require_client(request)
    return JSONResponse({"key_id": client_id, **await acl.get_usage(client_id)})


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    client_id = await _require_client(request)
    body = await request.json()
    # The client's model name is cosmetic *unless* it names a known alias
    # (see _resolve_target): plain clients (OpenCode, home-website) keep
    # getting whichever model is currently active (set from /admin, default
    # gateway's configured default_model) regardless of what "local" name
    # they sent -- this is what makes "looks like a local LLM" work without
    # every client needing to know the real upstream model string, and lets
    # the admin switch models live without redeploying anything. A client
    # that names an alias instead gets pinned to that alias's own
    # gateway+model.
    gw, current_model = await _resolve_target(body.get("model") or "")
    body["model"] = current_model
    stream = bool(body.get("stream"))
    headers = gateway.request_headers(gw)

    if not stream:
        await redis_client.incr_inflight()
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(gw["base_url"], headers=headers, json=body)
            try:
                data = resp.json()
            except ValueError:
                # Upstream didn't return JSON (e.g. a proxy/edge error page)
                # -- surface it as an error instead of crashing with an
                # unhandled 500 on .json().
                return JSONResponse(
                    {"error": {"message": resp.text[:2000], "type": "upstream_error"}},
                    status_code=resp.status_code if resp.status_code >= 400 else 502,
                )
            resp_usage = data.get("usage")
            if resp_usage:
                await redis_client.set_last_usage(current_model, resp_usage)
                await acl.record_usage(client_id, resp_usage)
            return JSONResponse(data, status_code=resp.status_code)
        finally:
            await redis_client.decr_inflight()

    return StreamingResponse(
        _stream_and_tap(headers, body, current_model, gw["base_url"], client_id), media_type="text/event-stream"
    )


async def _stream_and_tap(headers: dict, body: dict, model: str, base_url: str, client_id: str):
    # Wraps the counter around the generator's actual lifetime, not the
    # route function's -- FastAPI starts iterating this *after*
    # chat_completions() has already returned the StreamingResponse object,
    # so incr/decr belongs here, not in the route handler's own try/finally.
    last_usage = None
    await redis_client.incr_inflight()
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", base_url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    # Not SSE-shaped, but good enough for a homelab relay --
                    # forward whatever error body Cloudflare sent as-is.
                    yield await resp.aread()
                    return
                async for raw_line in resp.aiter_lines():
                    if not raw_line:
                        continue
                    if raw_line.startswith("data: ") and raw_line.strip() != "data: [DONE]":
                        try:
                            chunk = json.loads(raw_line[len("data: "):])
                            usage = chunk.get("usage")
                            if usage and usage.get("total_tokens"):
                                last_usage = usage
                        except json.JSONDecodeError:
                            pass
                    yield f"{raw_line}\n\n".encode()
    finally:
        if last_usage:
            await redis_client.set_last_usage(model, last_usage)
            await acl.record_usage(client_id, last_usage)
        await redis_client.decr_inflight()
