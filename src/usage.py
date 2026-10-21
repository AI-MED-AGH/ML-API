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


class UsageMiddleware:
    """Counts each request that carried a valid key, once its reply has been sent.

    Plain ASGI on purpose: `@app.middleware("http")` relays the body through an in-memory stream, so a reply would count
    as sent (and leave the response budget) as soon as the middleware had it, not when the server had written it out."""

    def __init__(self, app, *, state, ttl_days: int):
        self.app = app
        self._state = state  # the application's state: the Redis client is only known once the app has been built
        self._ttl_days = ttl_days

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = 500

        async def watch_status(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, watch_status)
        finally:
            request_state = scope.get("state") or {}
            key_id = request_state.get("key_id")
            if key_id:  # requests without a valid key are nobody's usage
                await record_usage(
                    self._state.redis, key_id, request_state.get("model", "-"), status,
                    ttl_days=self._ttl_days, elapsed_ms=int((time.monotonic() - started) * 1000),
                )
