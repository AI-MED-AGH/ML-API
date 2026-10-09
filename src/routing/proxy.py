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


async def forward(
    session: aiohttp.ClientSession,
    base_url: str,
    payload: dict,
    *,
    total_timeout: float,
    connect_timeout: float,
    max_response_bytes: int = 50 * 1024 * 1024,
) -> UpstreamResponse:
    url = f"{base_url.rstrip('/')}/predict"
    timeout = aiohttp.ClientTimeout(total=total_timeout, connect=connect_timeout)
    try:
        async with session.post(url, json=payload, timeout=timeout) as response:
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
