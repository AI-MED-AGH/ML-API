import logging
import time

logger = logging.getLogger(__name__)


async def record_usage(redis, key_id: str, model: str, status: int, *, ttl_days: int, elapsed_ms: int) -> None:
    """One log line and a few counters per request, kept per key and UTC day. Never raises: counting must not cost a
    client their answer.

    The hash `usage:<day>:<key_id>` holds `<model>:requests`, `<model>:errors` (5xx), `<model>:limited` (429) and
    `last_used`. `-` stands for requests that ended before a model was identified."""
    logger.info("request key=%s model=%s status=%s ms=%s", key_id, model, status, elapsed_ms)
    if ttl_days <= 0:
        return
    now = time.time()
    name = f"usage:{time.strftime('%Y%m%d', time.gmtime(now))}:{key_id}"
    try:
        pipe = redis.pipeline(transaction=False)
        pipe.hincrby(name, f"{model}:requests", 1)
        if status == 429:
            pipe.hincrby(name, f"{model}:limited", 1)
        elif status >= 500:
            pipe.hincrby(name, f"{model}:errors", 1)
        pipe.hset(name, "last_used", int(now))
        pipe.expire(name, ttl_days * 86400)
        await pipe.execute()
    except Exception:
        logger.warning("Could not record usage", exc_info=True)
