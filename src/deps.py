import aiohttp
from fastapi import Request

from src.auth.keys import AuthContext
from src.auth.store import KeyStore
from src.config import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_redis(request: Request):
    return request.app.state.redis


def get_session(request: Request) -> aiohttp.ClientSession:
    return request.app.state.session


async def get_auth(request: Request) -> AuthContext:
    return await KeyStore(get_redis(request)).authenticate(
        request.headers.get("X-API-Key")
    )
