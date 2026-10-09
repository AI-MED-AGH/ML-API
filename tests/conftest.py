import aiohttp
import httpx
import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis

from src.config import Settings


@pytest.fixture
def settings():
    return Settings(redis_url="redis://localhost:6379/0", _env_file=None)


@pytest_asyncio.fixture
async def redis():
    r = FakeAsyncRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest_asyncio.fixture
async def http_session():
    async with aiohttp.ClientSession() as session:
        yield session


@pytest_asyncio.fixture
async def client(settings, redis, http_session):
    from src.main import create_app

    app = create_app(settings, redis=redis, session=http_session)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://router") as c:
        yield c
