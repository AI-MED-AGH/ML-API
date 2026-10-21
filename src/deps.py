import aiohttp
from fastapi import Depends, Request

from src.auth.keys import AuthContext
from src.auth.store import KeyStore
from src.common.errors import RateLimited
from src.config import Settings
from src.limits import ConcurrencyGate
from src.ratelimit import RateLimiter
from src.routing.budget import ResponseBudget


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_redis(request: Request):
    return request.app.state.redis


def get_budget(request: Request) -> ResponseBudget:
    return request.app.state.budget


def get_session(request: Request) -> aiohttp.ClientSession:
    return request.app.state.session


async def get_auth(request: Request) -> AuthContext:
    auth = await KeyStore(get_redis(request)).authenticate(request.headers.get("X-API-Key"))
    request.state.key_id = auth.key_id  # read by the usage middleware
    return auth


def get_gate(request: Request) -> ConcurrencyGate:
    return request.app.state.gate


def rate_limited(scope: str, setting: str, key_field: str, window: int):
    """Dependency: authenticates, then counts the request against the key's budget before any body is read.

    Wrong keys never reach the counter, so nobody can use up another client's budget."""

    async def dependency(request: Request, auth: AuthContext = Depends(get_auth)) -> AuthContext:
        limit = auth.limits.get(key_field, getattr(get_settings(request), setting))  # an admin's per-key value wins
        retry_after = await RateLimiter(get_redis(request)).hit(scope, auth.key_id, limit=limit, window=window)
        if retry_after is not None:
            raise RateLimited("Rate limit exceeded", headers={"Retry-After": str(retry_after)})
        return auth

    return dependency
