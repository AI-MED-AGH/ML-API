import json
import logging
import re
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from src.api.predict import MODEL_NAME_RE, parse_predict_body, read_limited_body
from src.auth.keys import AuthContext
from src.common.errors import (
    ModelNotFound,
    ModelUnavailable,
    RateLimited,
    RedisUnavailable,
    UpstreamError,
    WrongMode,
)
from src.deps import get_budget, get_gate, get_redis, get_session, get_settings, rate_limited
from src.limits import enforce_model_limits
from src.routing.proxy import FULL_PARSE_LIMIT, HeldResponse, UpstreamResponse, call
from src.routing.routes import RouteStore

logger = logging.getLogger(__name__)
router = APIRouter()
_TERMINAL = ("succeeded", "failed")

JOB_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
_FAILED_JOB_BODY = b'{"job_id":"%s","status":"failed","error":"The job failed","error_type":"JobFailed"}'


def _not_ready(settings, state: str) -> ModelUnavailable:
    return ModelUnavailable(
        "Model is not ready",
        headers={"Retry-After": str(settings.retry_after_seconds), "X-Model-Status": state},
    )


def _passthrough(upstream: UpstreamResponse, body: bytes | None = None) -> HeldResponse:
    headers = {"Retry-After": upstream.retry_after} if upstream.retry_after else None
    return HeldResponse(
        content=upstream.body if body is None else body,
        status_code=upstream.status,
        media_type="application/json",
        headers=headers,
        release=upstream.release,
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


def _rewrite_job(body: bytes, model_id: str, public_id: str, *, hide_errors: bool = False) -> tuple[bytes, str | None]:
    """Clients only ever see the public id (`<model>~<id>`); other bodies pass through untouched.
    Also returns the job's status, when the body says one."""
    if len(body) > FULL_PARSE_LIMIT:
        # A big result: parsing it would stall the replica. job_id and status come first in the reply, so edit the head.
        head, tail = body[:1024], body[1024:]
        status = re.search(rb'"status"\s*:\s*"([a-z]+)"', head)
        head = re.sub(
            rb'("job_id"\s*:\s*")' + re.escape(model_id.encode()) + rb'"',
            lambda m: m.group(1) + public_id.encode() + b'"',
            head,
            count=1,
        )
        status = status.group(1).decode() if status else None
        if hide_errors and status == "failed":
            # the error text could be anywhere in a body this size: send a generic one instead of searching for it
            return _FAILED_JOB_BODY % public_id.encode(), status
        return head + tail, status
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return body, None
    if not isinstance(data, dict):
        return body, None
    status = data.get("status") if isinstance(data.get("status"), str) else None
    changed = False
    if data.get("job_id") == model_id:
        data["job_id"], changed = public_id, True
    if hide_errors and status == "failed" and data.get("error") is not None:
        data["error"], changed = "The job failed", True  # error_type stays: it names the kind of failure, not the internals
    return (json.dumps(data).encode() if changed else body), status


def _outstanding_key(key_id: str) -> str:
    return f"outstanding:{key_id}"


async def _check_outstanding(redis, auth: AuthContext, settings) -> int:
    """Refuses a new job while the key already has its allowance of unfinished ones. Returns the cap (0 = none)."""
    limit = auth.limits.get("max_outstanding_jobs", settings.max_outstanding_jobs)
    if limit <= 0:
        return 0
    try:
        key = _outstanding_key(auth.key_id)
        await redis.zremrangebyscore(key, "-inf", time.time())  # forget jobs whose record has expired
        if await redis.zcard(key) >= limit:
            raise RateLimited(
                "Too many unfinished jobs; wait for some to complete",
                headers={"Retry-After": str(settings.retry_after_seconds)},
            )
    except RedisError:
        raise RedisUnavailable("Authentication backend unavailable")
    return limit


@router.post(
    "/jobs",
    status_code=202,
    dependencies=[Depends(rate_limited("predict", "rate_limit_per_minute", "rpm", 60))],
)
async def submit_job(
    request: Request, auth: AuthContext = Depends(rate_limited("job-submit", "job_submit_limit_per_day", "jobs_per_day", 86400))
):
    settings = get_settings(request)
    redis = get_redis(request)

    raw = await read_limited_body(request, settings.max_body_bytes)
    model, payload = parse_predict_body(raw)

    if not auth.can_use(model):
        raise ModelNotFound("Model not found")
    route = await RouteStore(redis).get(model)
    if route is None:
        raise ModelNotFound("Model not found")
    request.state.model = model  # only real models, so a client cannot invent usage entries
    if route.mode != "queue":
        raise WrongMode("This model is synchronous; use POST /predict")
    await enforce_model_limits(request, auth, model, route)
    if route.state != "ready":
        raise _not_ready(settings, route.state)
    tracked = await _check_outstanding(redis, auth, settings)

    async with get_gate(request).slot(model, route.limits.get("max_concurrency", settings.model_max_concurrency)):
        upstream = await call(
            get_session(request), "POST", f"{route.url.rstrip('/')}/jobs", payload,
            total_timeout=settings.upstream_timeout,
            connect_timeout=settings.upstream_connect_timeout,
            max_response_bytes=settings.max_response_bytes,
            budget=get_budget(request),
            expose_errors=settings.expose_upstream_errors,
        )
    if not 200 <= upstream.status < 300:
        return _passthrough(upstream)

    try:
        model_job_id = _extract_job_id(upstream.body)
    finally:
        upstream.release()  # only the id is kept: the reply itself is not sent on
    public_id = f"{model}~{model_job_id}"
    try:
        # NX: a model that answers with an id that already exists must not take that job over from its owner
        claimed = await redis.set(f"jobowner:{public_id}", auth.key_id, ex=settings.job_owner_ttl, nx=True)
        if not claimed:
            existing = await redis.get(f"jobowner:{public_id}")
            if existing != auth.key_id:
                raise UpstreamError("Model returned a job id that already exists")
            await redis.expire(f"jobowner:{public_id}", settings.job_owner_ttl)
        if tracked:
            key = _outstanding_key(auth.key_id)
            await redis.zadd(key, {public_id: time.time() + settings.job_owner_ttl})
            await redis.expire(key, settings.job_owner_ttl)
    except RedisError:
        raise RedisUnavailable("Authentication backend unavailable")
    return JSONResponse({"job_id": public_id}, status_code=202)


@router.get("/jobs/{job_id:path}")
async def get_job(
    job_id: str,
    request: Request,
    auth: AuthContext = Depends(rate_limited("job-poll", "job_poll_limit_per_minute", "poll_rpm", 60)),
):
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
    request.state.model = model
    if route.state != "ready":
        raise _not_ready(settings, route.state)

    async with get_gate(request).slot(model, route.limits.get("max_concurrency", settings.model_max_concurrency)):
        upstream = await call(
            get_session(request), "GET", f"{route.url.rstrip('/')}/jobs/{model_job_id}",
            total_timeout=settings.upstream_timeout,
            connect_timeout=settings.upstream_connect_timeout,
            max_response_bytes=settings.max_response_bytes,
            budget=get_budget(request),
            expose_errors=settings.expose_upstream_errors,
        )
    try:
        body, status = _rewrite_job(upstream.body, model_job_id, job_id, hide_errors=not settings.expose_upstream_errors)
        if 200 <= upstream.status < 300 and status in _TERMINAL:
            try:
                await redis.zrem(_outstanding_key(auth.key_id), job_id)  # frees the slot; a failure only delays that until expiry
            except RedisError:
                logger.warning("Could not release a finished job's slot", exc_info=True)
        return _passthrough(upstream, body)
    except BaseException:
        upstream.release()
        raise
