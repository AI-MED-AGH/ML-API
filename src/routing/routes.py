import json
import logging
from dataclasses import dataclass

from redis.exceptions import RedisError

from src.common.errors import RedisUnavailable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Route:
    url: str
    state: str
    mode: str


class RouteStore:
    def __init__(self, redis):
        self._redis = redis

    async def get(self, model: str) -> Route | None:
        try:
            raw = await self._redis.get(f"route:{model}")
        except RedisError:
            logger.exception("Redis error while reading route")
            raise RedisUnavailable("Authentication backend unavailable")
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return Route(
                url=_require_str(data, "url"),
                state=_require_str(data, "state"),
                mode=_require_mode(data),
            )
        except (ValueError, TypeError):
            logger.warning("Malformed route record for a model")
            return None

    async def list_names(self) -> list[str]:
        try:
            names = [
                key.removeprefix("route:")
                async for key in self._redis.scan_iter(match="route:*")
            ]
        except RedisError:
            logger.exception("Redis error while listing routes")
            raise RedisUnavailable("Authentication backend unavailable")
        return sorted(names)


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
