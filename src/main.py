import logging
from contextlib import asynccontextmanager

import aiohttp
import redis.asyncio as aioredis
import time

from fastapi import FastAPI, Request

from src.api import catalog, health, jobs, predict
from src.common.errors import install_error_handlers
from src.config import Settings
from src.limits import ConcurrencyGate
from src.routing.budget import ResponseBudget
from src.usage import record_usage

logger = logging.getLogger(__name__)


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
            for close in owned:  # one failing close must not stop the others
                try:
                    await close()
                except Exception:
                    logger.exception("Error while shutting down")

    app = FastAPI(
        title="ML-Api router",
        description="Authenticated gateway to the hosted ML models.",
        version="2.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url="/redoc" if settings.enable_docs else None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )
    app.state.settings = settings
    app.state.redis = redis
    app.state.session = session
    app.state.gate = ConcurrencyGate()
    app.state.budget = ResponseBudget(settings.max_inflight_response_bytes)
    install_error_handlers(app)

    @app.middleware("http")
    async def count_usage(request: Request, call_next):
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            key_id = getattr(request.state, "key_id", None)
            if key_id:  # requests without a valid key are nobody's usage
                await record_usage(
                    app.state.redis, key_id, getattr(request.state, "model", "-"), status,
                    ttl_days=settings.usage_ttl_days, elapsed_ms=int((time.monotonic() - started) * 1000),
                )
    app.include_router(health.router)
    app.include_router(predict.router)
    app.include_router(jobs.router)
    app.include_router(catalog.router)
    return app
