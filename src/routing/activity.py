import logging
import time

logger = logging.getLogger(__name__)


class ActivityTracker:
    """Records `last_active:<model>` at most once per throttle window. Never raises."""

    def __init__(self, redis, throttle_seconds: int, clock=time.time):
        self._redis = redis
        self._throttle = throttle_seconds
        self._clock = clock

    async def touch(self, model: str) -> None:
        try:
            first = await self._redis.set(
                f"activity_throttle:{model}", 1, nx=True, ex=self._throttle
            )
            if first:
                await self._redis.set(f"last_active:{model}", int(self._clock()))
        except Exception:
            logger.warning("Could not record model activity", exc_info=True)
