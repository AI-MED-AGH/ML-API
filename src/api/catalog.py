import json
import logging

from fastapi import APIRouter, Depends, Request
from redis.exceptions import RedisError

from src.auth.keys import AuthContext
from src.common.errors import ModelNotFound, RedisUnavailable
from src.api.predict import MODEL_NAME_RE
from src.deps import get_auth, get_redis
from src.routing.routes import RouteStore

logger = logging.getLogger(__name__)
router = APIRouter()


async def _read_schema(redis, name: str) -> dict | None:
    try:
        raw = await redis.get(f"schema:{name}")
    except RedisError:
        raise RedisUnavailable("Authentication backend unavailable")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("Corrupt schema record")
        return None
    return data if isinstance(data, dict) else None


@router.get("/models")
async def list_models(request: Request, auth: AuthContext = Depends(get_auth)):
    redis = get_redis(request)
    store = RouteStore(redis)
    models = []
    for name in await store.list_names():
        if not auth.can_use(name):
            continue
        route = await store.get(name)
        if route is None:
            continue
        schema = await _read_schema(redis, name)
        models.append(
            {
                "name": name,
                "state": route.state,
                "version": schema.get("model_version") if schema else None,
            }
        )
    return {"models": models}


@router.get("/models/{name}/schema")
async def model_schema(name: str, request: Request, auth: AuthContext = Depends(get_auth)):
    if not MODEL_NAME_RE.fullmatch(name) or not auth.can_use(name):
        raise ModelNotFound("Model not found")
    schema = await _read_schema(get_redis(request), name)
    if schema is None:
        raise ModelNotFound("Model not found")
    return schema
