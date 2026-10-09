import logging

logger = logging.getLogger(__name__)


class WakeRequester:
    """Asks the Supervisor to wake a sleeping model. At most one request per TTL. Never raises."""

    def __init__(self, redis, pending_ttl_seconds: int):
        self._redis = redis
        self._ttl = pending_ttl_seconds

    async def request(self, model: str) -> bool:
        try:
            first = await self._redis.set(
                f"wake_pending:{model}", 1, nx=True, ex=self._ttl
            )
            if not first:
                return False
            await self._redis.lpush("wake", model)
            return True
        except Exception:
            logger.warning("Could not request model wake-up", exc_info=True)
            return False
