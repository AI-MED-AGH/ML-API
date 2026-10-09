import json
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from redis.exceptions import RedisError

from src.api.predict import MODEL_NAME_RE, parse_predict_body, read_limited_body
from src.auth.keys import AuthContext
from src.common.errors import (
    ModelNotFound,
    ModelUnavailable,
    RedisUnavailable,
    UpstreamError,
    WrongMode,
)
from src.deps import get_auth, get_redis, get_session, get_settings
from src.routing.proxy import UpstreamResponse, call
from src.routing.routes import RouteStore

router = APIRouter()

JOB_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")


def _not_ready(settings, state: str) -> ModelUnavailable:
    return ModelUnavailable(
        "Model is not ready",
        headers={"Retry-After": str(settings.retry_after_seconds), "X-Model-Status": state},
    )


def _passthrough(upstream: UpstreamResponse, body: bytes | None = None) -> Response:
    headers = {"Retry-After": upstream.retry_after} if upstream.retry_after else None
    return Response(
        content=upstream.body if body is None else body,
        status_code=upstream.status,
        media_type="application/json",
        headers=headers,
    )


def _extract_job_id(body: bytes) -> str:
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        data = None
    job_id = data.get("job_id") if isinstance(data, dict) else None
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise UpstreamError("Model returned an invalid job id")
    return job_id


def _rewrite_job_id(body: bytes, model_id: str, public_id: str) -> bytes:
    """Clients only ever see the public id (`<model>~<id>`); other bodies pass through untouched."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return body
    if isinstance(data, dict) and data.get("job_id") == model_id:
        data["job_id"] = public_id
        return json.dumps(data).encode()
    return body


@router.post("/jobs", status_code=202)
async def submit_job(request: Request, auth: AuthContext = Depends(get_auth)):
    settings = get_settings(request)
    redis = get_redis(request)

    raw = await read_limited_body(request, settings.max_body_bytes)
    model, payload = parse_predict_body(raw)

    if not auth.can_use(model):
        raise ModelNotFound("Model not found")
    route = await RouteStore(redis).get(model)
    if route is None:
        raise ModelNotFound("Model not found")
    if route.mode != "queue":
        raise WrongMode("This model is synchronous; use POST /predict")
    if route.state != "ready":
        raise _not_ready(settings, route.state)

    upstream = await call(
        get_session(request), "POST", f"{route.url.rstrip('/')}/jobs", payload,
        total_timeout=settings.upstream_timeout,
        connect_timeout=settings.upstream_connect_timeout,
        max_response_bytes=settings.max_response_bytes,
    )
    if not 200 <= upstream.status < 300:
        return _passthrough(upstream)

    model_job_id = _extract_job_id(upstream.body)
    public_id = f"{model}~{model_job_id}"
    try:
        # NX: a model that answers with an id that already exists must not take that job over from its owner
        claimed = await redis.set(f"jobowner:{public_id}", auth.key_id, ex=settings.job_owner_ttl, nx=True)
        if not claimed:
            existing = await redis.get(f"jobowner:{public_id}")
            if existing != auth.key_id:
                raise UpstreamError("Model returned a job id that already exists")
            await redis.expire(f"jobowner:{public_id}", settings.job_owner_ttl)
    except RedisError:
        raise RedisUnavailable("Authentication backend unavailable")
    return JSONResponse({"job_id": public_id}, status_code=202)


@router.get("/jobs/{job_id:path}")
async def get_job(job_id: str, request: Request, auth: AuthContext = Depends(get_auth)):
    settings = get_settings(request)
    redis = get_redis(request)

    model, separator, model_job_id = job_id.partition("~")
    if (
        not separator
        or not MODEL_NAME_RE.fullmatch(model)
        or not JOB_ID_RE.fullmatch(model_job_id)
        or not auth.can_use(model)
    ):
        raise ModelNotFound("Job not found")
    try:
        # polling keeps the ownership record alive for as long as the submitter still cares about the job
        owner = await redis.getex(f"jobowner:{job_id}", ex=settings.job_owner_ttl)
    except RedisError:
        raise RedisUnavailable("Authentication backend unavailable")
    if owner != auth.key_id:  # also covers unknown and expired jobs: no way to tell them apart
        raise ModelNotFound("Job not found")
    route = await RouteStore(redis).get(model)
    if route is None or route.mode != "queue":
        raise ModelNotFound("Job not found")
    if route.state != "ready":
        raise _not_ready(settings, route.state)

    upstream = await call(
        get_session(request), "GET", f"{route.url.rstrip('/')}/jobs/{model_job_id}",
        total_timeout=settings.upstream_timeout,
        connect_timeout=settings.upstream_connect_timeout,
        max_response_bytes=settings.max_response_bytes,
    )
    return _passthrough(upstream, _rewrite_job_id(upstream.body, model_job_id, job_id))
