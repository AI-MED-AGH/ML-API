import asyncio
import json
import logging
from typing import Any

import aiohttp

from src.common.errors import UpstreamError, UpstreamTimeout

logger = logging.getLogger(__name__)


async def forward(
    session: aiohttp.ClientSession,
    base_url: str,
    payload: dict,
    *,
    total_timeout: float,
    connect_timeout: float,
) -> tuple[int, Any]:
    url = f"{base_url.rstrip('/')}/predict"
    timeout = aiohttp.ClientTimeout(total=total_timeout, connect=connect_timeout)
    try:
        async with session.post(url, json=payload, timeout=timeout) as response:
            status = response.status
            try:
                # Parse the raw bytes ourselves: response.json() returns None for an empty body.
                body = json.loads(await response.read())
            except (ValueError, RecursionError):
                logger.warning("Upstream returned a non-JSON body (status %s)", status)
                raise UpstreamError("Model returned an invalid response")
            return status, body
    except asyncio.TimeoutError:
        raise UpstreamTimeout("Model did not respond in time")
    except aiohttp.ClientError:
        logger.warning("Upstream connection failed", exc_info=True)
        raise UpstreamError("Could not reach the model")
