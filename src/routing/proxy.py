import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp
from fastapi.responses import Response

from src.common.errors import RouterBusy, UpstreamError, UpstreamTimeout
from src.routing.budget import ResponseBudget

logger = logging.getLogger(__name__)


def _nothing() -> None:
    pass


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    body: bytes  # raw bytes, validated as JSON but never re-encoded
    retry_after: str | None = None
    # The body stays counted against the ResponseBudget until this is called (once is enough, more is harmless).
    # Pass it on with the body (HeldResponse), or call it when the body is not sent on.
    release: Callable[[], None] = _nothing


class HeldResponse(Response):
    """A reply that keeps its bytes counted against the budget until they have been handed to the server,
    so a client that reads slowly cannot hold memory the budget thinks is free."""

    def __init__(self, *args, release: Callable[[], None], **kwargs):
        self._release = release  # first: __del__ must find it even if the rest of __init__ fails
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()

    def __del__(self):
        self._release()  # a reply that was built but never sent must not keep the budget reserved for good


_RETRY_AFTER_RE = re.compile(r"[0-9]{1,10}", re.ASCII)

# Parsing JSON holds the GIL for the whole call (a thread would not help), so megabytes of it stall every other request
# on the replica. Above this size only the outer shape is checked.
FULL_PARSE_LIMIT = 1024 * 1024
_MODEL_ERROR_BODY = b'{"success":false,"error":"The model reported an error","error_type":"ModelError","details":{}}'
_BRACKETS = {ord("{"): ord("}"), ord("["): ord("]")}


def _looks_like_json(body: bytes) -> bool:
    if len(body) <= FULL_PARSE_LIMIT:
        try:
            json.loads(body)  # must be JSON (empty or HTML bodies are an upstream fault)
        except (ValueError, RecursionError):
            return False
        return True
    head, tail = body[:64].lstrip(), body[-64:].rstrip()  # slices: never copies the whole body
    return bool(head) and bool(tail) and _BRACKETS.get(head[0]) == tail[-1]


def _release_once(budget: ResponseBudget, size: int) -> Callable[[], None]:
    done = False

    def release() -> None:
        nonlocal done
        if not done:
            done = True
            budget.release(size)

    return release


def _safe_retry_after(value: str | None) -> str | None:
    """Only a plain number of seconds is passed on; anything else a model sends is dropped."""
    return value if value is not None and _RETRY_AFTER_RE.fullmatch(value) else None


async def call(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    payload: dict | None = None,
    *,
    total_timeout: float,
    connect_timeout: float,
    max_response_bytes: int = 50 * 1024 * 1024,
    budget: ResponseBudget | None = None,
    expose_errors: bool = True,
) -> UpstreamResponse:
    """One request to a model container. The reply must be JSON, but its bytes are passed back unchanged."""
    # sock_connect (not connect): waiting for a pooled connection must count against `total`, not the connect limit
    timeout = aiohttp.ClientTimeout(total=total_timeout, sock_connect=connect_timeout)
    kwargs = {"json": payload} if payload is not None else {}
    reserved = 0
    handed_over = False  # the reservation now belongs to the UpstreamResponse
    try:
        # never follow redirects: a model container must not be able to aim the Router at other hosts
        async with session.request(method, url, timeout=timeout, allow_redirects=False, **kwargs) as response:
            status = response.status
            if not 200 <= status <= 299 and not 400 <= status <= 599:
                logger.warning("Upstream answered with unusable status %s", status)
                raise UpstreamError("Model returned an unexpected response")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > max_response_bytes:
                    logger.warning("Upstream response exceeded %s bytes", max_response_bytes)
                    raise UpstreamError("Model response too large")
                if budget is not None:
                    if not budget.reserve(len(chunk)):
                        logger.warning("Router response budget exhausted; refusing a model reply")
                        raise RouterBusy("Router is busy; retry shortly", headers={"Retry-After": "1"})
                    reserved += len(chunk)
                chunks.append(chunk)
            body = b"".join(chunks)
            if not _looks_like_json(body):
                logger.warning("Upstream returned a non-JSON body (status %s)", status)
                raise UpstreamError("Model returned an invalid response")
            release = _nothing
            if status >= 500 and not expose_errors:
                body = _MODEL_ERROR_BODY  # the model's own text can name files and internals; the bytes read are dropped
            elif budget is not None:
                release, handed_over = _release_once(budget, reserved), True
            return UpstreamResponse(status, body, _safe_retry_after(response.headers.get("Retry-After")), release)
    except asyncio.TimeoutError:
        raise UpstreamTimeout("Model did not respond in time")
    except aiohttp.ClientError as exc:
        # the exception text can contain raw bytes the model sent: log only its type
        logger.warning("Upstream request failed: %s", type(exc).__name__)
        raise UpstreamError("Could not reach the model")
    finally:
        if budget is not None and not handed_over:
            budget.release(reserved)


async def forward(
    session: aiohttp.ClientSession,
    base_url: str,
    payload: dict,
    *,
    total_timeout: float,
    connect_timeout: float,
    max_response_bytes: int = 50 * 1024 * 1024,
    budget: ResponseBudget | None = None,
    expose_errors: bool = True,
) -> UpstreamResponse:
    """POST a prediction request to the model's /predict."""
    return await call(
        session, "POST", f"{base_url.rstrip('/')}/predict", payload,
        total_timeout=total_timeout, connect_timeout=connect_timeout, max_response_bytes=max_response_bytes,
        budget=budget, expose_errors=expose_errors,
    )
