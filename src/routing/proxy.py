import asyncio
import json
import logging
from dataclasses import dataclass

import aiohttp

from src.common.errors import UpstreamError, UpstreamTimeout

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    body: bytes  # raw bytes, validated as JSON but never re-encoded
    retry_after: str | None = None


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
    timeout = aiohttp.ClientTimeout(total=total_timeout, connect=connect_timeout)
    kwargs = {"json": payload} if payload is not None else {}
    try:
        async with session.request(method, url, timeout=timeout, **kwargs) as response:
            status = response.status
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
            return UpstreamResponse(status, body, response.headers.get("Retry-After"))
    except asyncio.TimeoutError:
        raise UpstreamTimeout("Model did not respond in time")
    except aiohttp.ClientError:
        logger.warning("Upstream connection failed", exc_info=True)
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
