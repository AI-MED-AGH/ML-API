import logging
import math
import time

from redis.exceptions import RedisError

from src.common.errors import RedisUnavailable

logger = logging.getLogger(__name__)


class RateLimiter:
    """Fixed-window counters in Redis, shared by every Router replica."""

    def __init__(self, redis, clock=time.time):
        self._redis = redis
        self._clock = clock

    async def hit(self, scope: str, subject: str, *, limit: int, window: int) -> int | None:
        """Counts one request. Returns None if allowed, else the seconds until the window resets. limit 0 = unlimited."""
        if limit <= 0:
            return None
        now = self._clock()
        index = int(now // window)
        key = f"ratelimit:{scope}:{subject}:{index}"
        try:
            pipe = self._redis.pipeline(transaction=True)
            pipe.incr(key)
            pipe.expire(key, window)  # the counter cleans itself up; the window index keeps windows apart
            count, _ = await pipe.execute()
        except RedisError:
            logger.exception("Redis error while counting a request")
            raise RedisUnavailable("Authentication backend unavailable")
        if count <= limit:
            return None
        return max(1, math.ceil((index + 1) * window - now))
