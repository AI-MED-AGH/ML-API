import httpx
import pytest
from fastapi import FastAPI

from src.common.errors import (
    ModelNotFound,
    ModelUnavailable,
    install_error_handlers,
)


@pytest.fixture
def app():
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/not-found")
    async def not_found():
        raise ModelNotFound("Model not found")

    @app.get("/unavailable")
    async def unavailable():
        raise ModelUnavailable("starting", headers={"Retry-After": "5"})

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret internal detail")

    @app.get("/typed/{n}")
    async def typed(n: int):
        return {"n": n}

    return app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_api_error_uses_error_response_shape(client):
    r = await client.get("/not-found")
    assert r.status_code == 404
    assert r.json() == {
        "success": False,
        "error": "Model not found",
        "error_type": "ModelNotFound",
        "details": {},
    }


async def test_api_error_headers_are_sent(client):
    r = await client.get("/unavailable")
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"
    assert r.json()["error_type"] == "ModelUnavailable"


async def test_unexpected_error_leaks_nothing(client):
    r = await client.get("/boom")
    assert r.status_code == 500
    assert "secret internal detail" not in r.text
    assert r.json()["error_type"] == "InternalError"


async def test_validation_error_is_422_with_our_shape(client):
    r = await client.get("/typed/abc")
    assert r.status_code == 422
    assert r.json()["error_type"] == "ValidationError"


async def test_unknown_path_uses_error_shape(client):
    r = await client.get("/nope")
    assert r.status_code == 404
    assert r.json()["success"] is False
