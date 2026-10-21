import json
import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from redis.exceptions import RedisError

from src.common.errors import RedisUnavailable
from src.limits import MODEL_FIELDS, parse_limits

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Route:
    url: str
    state: str
    mode: str
    limits: dict[str, int] = field(default_factory=dict)


class RouteStore:
    def __init__(self, redis):
        self._redis = redis

    async def get(self, model: str) -> Route | None:
        try:
            raw = await self._redis.get(f"route:{model}")
        except RedisError:
            logger.exception("Redis error while reading route")
            raise RedisUnavailable("Authentication backend unavailable")
        return _parse_route(raw)

    async def get_many(self, names: list[str]) -> dict[str, Route]:
        """The routes that exist and parse, read in one round trip."""
        if not names:
            return {}
        try:
            raws = await self._redis.mget([f"route:{n}" for n in names])
        except RedisError:
            logger.exception("Redis error while reading routes")
            raise RedisUnavailable("Authentication backend unavailable")
        parsed = {name: _parse_route(raw) for name, raw in zip(names, raws)}
        return {name: route for name, route in parsed.items() if route is not None}

    async def list_names(self) -> list[str]:
        try:
            if await self._redis.exists("route_index_ready"):
                # the Supervisor maintains `route_index`: no walk over every key in Redis
                return sorted(await self._redis.smembers("route_index"))
            names = [
                key.removeprefix("route:")
                async for key in self._redis.scan_iter(match="route:*")
            ]
        except RedisError:
            logger.exception("Redis error while listing routes")
            raise RedisUnavailable("Authentication backend unavailable")
        return sorted(names)


def _parse_route(raw: str | None) -> Route | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return Route(
            url=_require_http_url(data),
            state=_require_str(data, "state"),
            mode=_require_mode(data),
            limits=parse_limits(data.get("limits"), MODEL_FIELDS),
        )
    except (ValueError, TypeError):
        logger.warning("Malformed route record for a model")
        return None


def _require_str(data: dict, field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _require_mode(data: dict) -> str:
    mode = data.get("mode", "sync")
    if mode not in ("sync", "queue"):
        raise ValueError("mode must be 'sync' or 'queue'")
    return mode


def _require_http_url(data: dict) -> str:
    url = _require_str(data, "url")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("url must be an http(s) URL with a host")
    return url
