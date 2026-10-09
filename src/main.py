import logging
from contextlib import asynccontextmanager

import aiohttp
import redis.asyncio as aioredis
from fastapi import FastAPI

from src.api import catalog, health, predict
from src.common.errors import install_error_handlers
from src.config import Settings


def create_app(settings: Settings | None = None, *, redis=None, session=None) -> FastAPI:
    settings = settings or Settings()
    logging.basicConfig(level=settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned = []
        if app.state.redis is None:
            app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
            owned.append(app.state.redis.aclose)
        if app.state.session is None:
            app.state.session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    limit=settings.upstream_max_connections,
                    limit_per_host=settings.upstream_max_connections_per_host,
                )
            )
            owned.append(app.state.session.close)
        try:
            yield
        finally:
            for close in owned:
                await close()

    app = FastAPI(
        title="ML-Api router",
        description="Authenticated gateway to the hosted ML models.",
        version="2.0.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.redis = redis
    app.state.session = session
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(predict.router)
    app.include_router(catalog.router)
    return app
