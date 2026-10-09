import json
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from src.auth.keys import AuthContext
from src.common.errors import (
    ModelNotFound,
    ModelUnavailable,
    PayloadTooLarge,
    RequestInvalid,
    WrongMode,
)
from src.deps import get_auth, get_redis, get_session, get_settings
from src.routing.activity import ActivityTracker
from src.routing.proxy import forward
from src.routing.routes import RouteStore
from src.routing.wake import WakeRequester

router = APIRouter()

MODEL_NAME_RE = re.compile(r"[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?")


async def read_limited_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLarge("Request body too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise PayloadTooLarge("Request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_predict_body(raw: bytes) -> tuple[str, dict]:
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise RequestInvalid("Body must be valid JSON")
    if not isinstance(body, dict):
        raise RequestInvalid("Body must be a JSON object")
    model = body.get("model")
    if not isinstance(model, str) or not MODEL_NAME_RE.fullmatch(model):
        raise RequestInvalid("'model' must be a valid model name")
    if "data" not in body:
        raise RequestInvalid("'data' is required")
    return model, {k: v for k, v in body.items() if k != "model"}


@router.post("/predict")
async def predict(request: Request, auth: AuthContext = Depends(get_auth)):
    settings = get_settings(request)
    redis = get_redis(request)

    raw = await read_limited_body(request, settings.max_body_bytes)
    model, payload = parse_predict_body(raw)

    if not auth.can_use(model):
        raise ModelNotFound("Model not found")
    route = await RouteStore(redis).get(model)
    if route is None:
        raise ModelNotFound("Model not found")
    if route.mode != "sync":
        raise WrongMode("This model only accepts jobs; use POST /jobs")

    await ActivityTracker(redis, settings.activity_throttle_seconds).touch(model)

    if route.state != "ready":
        if route.state == "sleeping":
            await WakeRequester(redis, settings.wake_pending_ttl_seconds).request(model)
        raise ModelUnavailable(
            "Model is not ready",
            headers={
                "Retry-After": str(settings.retry_after_seconds),
                "X-Model-Status": route.state,
            },
        )

    upstream = await forward(
        get_session(request),
        route.url,
        payload,
        total_timeout=settings.upstream_timeout,
        connect_timeout=settings.upstream_connect_timeout,
        max_response_bytes=settings.max_response_bytes,
    )
    headers = {"Retry-After": upstream.retry_after} if upstream.retry_after else None
    return Response(
        content=upstream.body,
        status_code=upstream.status,
        media_type="application/json",
        headers=headers,
    )
