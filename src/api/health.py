from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.deps import get_redis

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    try:
        await get_redis(request).ping()
    except Exception:
        return JSONResponse({"status": "degraded"}, status_code=503)
    return {"status": "ok"}
