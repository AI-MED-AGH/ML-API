import asyncio
import json
import logging
import re
from dataclasses import dataclass

import aiohttp

from src.common.errors import UpstreamError, UpstreamTimeout

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    body: bytes  # raw bytes, validated as JSON but never re-encoded
    retry_after: str | None = None


_RETRY_AFTER_RE = re.compile(r"[0-9]{1,10}", re.ASCII)


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
) -> UpstreamResponse:
    """One request to a model container. The reply must be JSON, but its bytes are passed back unchanged."""
    # sock_connect (not connect): waiting for a pooled connection must count against `total`, not the connect limit
    timeout = aiohttp.ClientTimeout(total=total_timeout, sock_connect=connect_timeout)
    kwargs = {"json": payload} if payload is not None else {}
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
                chunks.append(chunk)
            body = b"".join(chunks)
            try:
                json.loads(body)  # must be JSON (empty or HTML bodies are an upstream fault)
            except (ValueError, RecursionError):
                logger.warning("Upstream returned a non-JSON body (status %s)", status)
                raise UpstreamError("Model returned an invalid response")
            return UpstreamResponse(status, body, _safe_retry_after(response.headers.get("Retry-After")))
    except asyncio.TimeoutError:
        raise UpstreamTimeout("Model did not respond in time")
    except aiohttp.ClientError as exc:
        # the exception text can contain raw bytes the model sent: log only its type
        logger.warning("Upstream request failed: %s", type(exc).__name__)
        raise UpstreamError("Could not reach the model")


async def forward(
    session: aiohttp.ClientSession,
    base_url: str,
    payload: dict,
    *,
    total_timeout: float,
    connect_timeout: float,
    max_response_bytes: int = 50 * 1024 * 1024,
) -> UpstreamResponse:
    """POST a prediction request to the model's /predict."""
    return await call(
        session, "POST", f"{base_url.rstrip('/')}/predict", payload,
        total_timeout=total_timeout, connect_timeout=connect_timeout, max_response_bytes=max_response_bytes,
    )
