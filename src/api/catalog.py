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


def _parse_schema(raw: str | None) -> dict | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("Corrupt schema record")
        return None
    return data if isinstance(data, dict) else None


async def _read_schema(redis, name: str) -> dict | None:
    try:
        raw = await redis.get(f"schema:{name}")
    except RedisError:
        raise RedisUnavailable("Authentication backend unavailable")
    return _parse_schema(raw)


@router.get("/models")
async def list_models(request: Request, auth: AuthContext = Depends(get_auth)):
    redis = get_redis(request)
    store = RouteStore(redis)
    names = [name for name in await store.list_names() if auth.can_use(name)]
    routes = await store.get_many(names)
    names = [name for name in names if name in routes]
    try:
        raw_schemas = await redis.mget([f"schema:{name}" for name in names]) if names else []
    except RedisError:
        raise RedisUnavailable("Authentication backend unavailable")
    models = []
    for name, raw in zip(names, raw_schemas):
        schema = _parse_schema(raw)
        models.append(
            {
                "name": name,
                "state": routes[name].state,
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
