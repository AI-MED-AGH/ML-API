import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.deps import get_redis, get_settings

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    """Unauthenticated, so the Redis check is reused for a moment instead of being repeated for every caller."""
    ttl = get_settings(request).health_cache_seconds
    now = time.monotonic()
    cached = getattr(request.app.state, "health_cache", None)
    if ttl > 0 and cached is not None and now - cached[0] < ttl:
        healthy = cached[1]
    else:
        try:
            await get_redis(request).ping()
            healthy = True
        except Exception:
            healthy = False
        request.app.state.health_cache = (now, healthy)
    if not healthy:
        return JSONResponse({"status": "degraded"}, status_code=503)
    return {"status": "ok"}
