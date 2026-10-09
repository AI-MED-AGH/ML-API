import json

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from src.main import create_app
from tests.helpers import new_key, store_key, store_route


async def seed(redis, upstream=None, models=("m1",), **route_kw):
    key = await store_key(redis, allowed_models=models)
    if upstream is not None:
        route_kw.setdefault("url", upstream.url)
    await store_route(redis, "m1", **route_kw)
    return {"X-API-Key": key}


def make_client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://r")


async def test_proxies_body_without_model_field(client, redis, upstream):
    headers = await seed(redis, upstream)
    upstream.payload = {"success": True, "prediction": 7}
    r = await client.post(
        "/predict",
        headers=headers,
        json={"model": "m1", "data": {"f": [1, 2]}, "metadata": {"trace": "t"}, "extra": 1},
    )
    assert r.status_code == 200
    assert r.json() == {"success": True, "prediction": 7}
    assert upstream.requests == [{"data": {"f": [1, 2]}, "metadata": {"trace": "t"}, "extra": 1}]


async def test_upstream_error_status_and_body_pass_through(client, redis, upstream):
    headers = await seed(redis, upstream)
    upstream.status, upstream.payload = 422, {"error": "bad features"}
    r = await client.post("/predict", headers=headers, json={"model": "m1", "data": 1})
    assert r.status_code == 422
    assert r.json() == {"error": "bad features"}


async def test_data_null_is_allowed(client, redis, upstream):
    headers = await seed(redis, upstream)
    r = await client.post("/predict", headers=headers, json={"model": "m1", "data": None})
    assert r.status_code == 200


async def test_predict_on_queue_mode_model_is_400(client, redis, upstream):
    headers = await seed(redis, upstream, mode="queue")
    r = await client.post("/predict", headers=headers, json={"model": "m1", "data": 1})
    assert r.status_code == 400
    assert r.json()["error_type"] == "WrongMode"
    assert upstream.requests == []


async def test_all_auth_failures_have_identical_responses(client, redis, upstream):
    headers = await seed(redis, upstream)
    good = headers["X-API-Key"]
    wrong_secret = good[:-2] + ("AA" if not good.endswith("AA") else "BB")
    _, _, unknown_id = new_key()
    bodies = set()
    for h in [{}, {"X-API-Key": "junk"}, {"X-API-Key": unknown_id}, {"X-API-Key": wrong_secret}]:
        r = await client.post("/predict", headers=h, json={"model": "m1", "data": 1})
        assert r.status_code == 401
        bodies.add(r.text)
    assert len(bodies) == 1


async def test_out_of_scope_and_unknown_model_are_identical_404(client, redis, upstream):
    headers = await seed(redis, upstream, models=("m1",))
    await store_route(redis, "secret-model", url=upstream.url)  # exists, but not in scope
    r1 = await client.post("/predict", headers=headers, json={"model": "secret-model", "data": 1})
    r2 = await client.post("/predict", headers=headers, json={"model": "does-not-exist", "data": 1})
    assert r1.status_code == r2.status_code == 404
    assert r1.text == r2.text
    assert r1.json()["error_type"] == "ModelNotFound"


async def test_pattern_and_allow_all_scopes(client, redis, upstream):
    await store_route(redis, "ecg-1", url=upstream.url)
    pat = await store_key(redis, allowed_models=("ecg-*",))
    allow = await store_key(redis, allowed_models=(), allow_all=True)
    for key in (pat, allow):
        r = await client.post(
            "/predict", headers={"X-API-Key": key}, json={"model": "ecg-1", "data": 1}
        )
        assert r.status_code == 200


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[1,2,3]",
        b'"just a string"',
        b"{}",
        json.dumps({"model": "m1"}).encode(),  # missing data
        json.dumps({"model": 5, "data": 1}).encode(),
        json.dumps({"model": "../etc", "data": 1}).encode(),
        json.dumps({"model": "a:b", "data": 1}).encode(),
        json.dumps({"model": "x" * 100, "data": 1}).encode(),
        json.dumps({"model": "UPPER", "data": 1}).encode(),
        b"[" * 100_000,  # RecursionError in json.loads
        b"\xff\xfe\x00",
    ],
)
async def test_hostile_bodies_are_422(client, redis, upstream, content):
    headers = await seed(redis, upstream)
    r = await client.post(
        "/predict", headers={**headers, "Content-Type": "application/json"}, content=content
    )
    assert r.status_code == 422
    assert r.json()["error_type"] == "ValidationError"
    assert upstream.requests == []


async def test_unauthenticated_requests_are_rejected_before_body_validation(client):
    r = await client.post("/predict", content=b"not json")
    assert r.status_code == 401


async def test_oversized_body_with_content_length_is_413(redis, http_session, settings, upstream):
    settings.max_body_bytes = 100
    app = create_app(settings, redis=redis, session=http_session)
    headers = await seed(redis, upstream)
    async with make_client(app) as c:
        r = await c.post("/predict", headers=headers, json={"model": "m1", "data": "x" * 500})
    assert r.status_code == 413
    assert r.json()["error_type"] == "PayloadTooLarge"


async def test_oversized_chunked_body_is_413(redis, http_session, settings, upstream):
    settings.max_body_bytes = 100
    app = create_app(settings, redis=redis, session=http_session)
    headers = await seed(redis, upstream)

    async def chunks():
        for _ in range(10):
            yield b"x" * 50

    async with make_client(app) as c:
        r = await c.post("/predict", headers=headers, content=chunks())
    assert r.status_code == 413


@pytest.mark.parametrize("state", ["sleeping", "starting", "waiting_for_gpu", "unavailable"])
async def test_not_ready_model_returns_503_with_status_headers(client, redis, state):
    headers = await seed(redis, state=state)
    r = await client.post("/predict", headers=headers, json={"model": "m1", "data": 1})
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"
    assert r.headers["x-model-status"] == state
    assert r.json()["error_type"] == "ModelUnavailable"


async def test_sleeping_model_enqueues_exactly_one_wake(client, redis):
    headers = await seed(redis, state="sleeping")
    for _ in range(3):
        await client.post("/predict", headers=headers, json={"model": "m1", "data": 1})
    assert await redis.lrange("wake", 0, -1) == ["m1"]


async def test_starting_model_does_not_enqueue_wake(client, redis):
    headers = await seed(redis, state="starting")
    await client.post("/predict", headers=headers, json={"model": "m1", "data": 1})
    assert await redis.llen("wake") == 0


async def test_successful_predict_records_activity(client, redis, upstream):
    headers = await seed(redis, upstream)
    await client.post("/predict", headers=headers, json={"model": "m1", "data": 1})
    assert await redis.get("last_active:m1") is not None


async def test_upstream_timeout_is_504(redis, http_session, settings, upstream):
    settings.upstream_timeout = 0.1
    upstream.delay = 1.0
    app = create_app(settings, redis=redis, session=http_session)
    headers = await seed(redis, upstream)
    async with make_client(app) as c:
        r = await c.post("/predict", headers=headers, json={"model": "m1", "data": 1})
    assert r.status_code == 504
    assert r.json()["error_type"] == "UpstreamTimeout"


async def test_unreachable_upstream_is_502(client, redis):
    headers = await seed(redis, url="http://127.0.0.1:1")
    r = await client.post("/predict", headers=headers, json={"model": "m1", "data": 1})
    assert r.status_code == 502
    assert r.json()["error_type"] == "UpstreamError"


async def test_non_json_upstream_is_502(client, redis, upstream):
    headers = await seed(redis, upstream)
    upstream.body = b"<html>oops</html>"
    r = await client.post("/predict", headers=headers, json={"model": "m1", "data": 1})
    assert r.status_code == 502
    assert "oops" not in r.text


class FlakyWrites:
    """Reads work; the writes used for activity/wake fail."""

    def __init__(self, inner):
        self._inner = inner

    async def get(self, *a, **k):
        return await self._inner.get(*a, **k)

    def scan_iter(self, *a, **k):
        return self._inner.scan_iter(*a, **k)

    async def set(self, *a, **k):
        raise RedisConnectionError("write failed")

    async def lpush(self, *a, **k):
        raise RedisConnectionError("write failed")


async def test_activity_write_failure_does_not_fail_predict(
    redis, http_session, settings, upstream
):
    headers = await seed(redis, upstream)
    app = create_app(settings, redis=FlakyWrites(redis), session=http_session)
    async with make_client(app) as c:
        r = await c.post("/predict", headers=headers, json={"model": "m1", "data": 1})
    assert r.status_code == 200


class DeadRedis:
    async def get(self, *a, **k):
        raise RedisConnectionError("down")

    async def ping(self):
        raise RedisConnectionError("down")


async def test_redis_down_is_503_on_predict(http_session, settings):
    _, _, key = new_key()
    app = create_app(settings, redis=DeadRedis(), session=http_session)
    async with make_client(app) as c:
        r = await c.post(
            "/predict", headers={"X-API-Key": key}, json={"model": "m1", "data": 1}
        )
    assert r.status_code == 503
    assert r.json()["error_type"] == "AuthBackendUnavailable"


async def test_health_ok(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_health_degraded_when_redis_down(http_session, settings):
    app = create_app(settings, redis=DeadRedis(), session=http_session)
    async with make_client(app) as c:
        r = await c.get("/health")
    assert r.status_code == 503
    assert r.json() == {"status": "degraded"}
