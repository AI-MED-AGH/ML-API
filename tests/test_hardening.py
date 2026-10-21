import json

import httpx
import pytest

from src.config import Settings
from src.main import create_app
from tests.helpers import store_key, store_route

LEAK = "KeyError: '/srv/weights/model.pkl' line 42"


async def make_client(redis, http_session, **overrides):
    settings = Settings(redis_url="redis://x/0", _env_file=None, **overrides)
    app = create_app(settings, redis=redis, session=http_session)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://r")


# ── model error text does not reach clients ──


async def test_model_5xx_bodies_are_replaced_but_status_and_retry_after_stay(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url)
    key = await store_key(redis)
    upstream.status, upstream.payload, upstream.headers = 500, {"error": LEAK, "error_type": "KeyError"}, {"Retry-After": "9"}
    async with await make_client(redis, http_session) as client:
        r = await client.post("/predict", headers={"X-API-Key": key}, json={"model": "m1", "data": 1})
    assert r.status_code == 500
    assert r.headers["retry-after"] == "9"
    assert r.json()["error_type"] == "ModelError"
    assert "weights" not in r.text


async def test_model_4xx_bodies_still_pass_through(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url)
    key = await store_key(redis)
    upstream.status, upstream.payload = 422, {"detail": "values must not be empty"}
    async with await make_client(redis, http_session) as client:
        r = await client.post("/predict", headers={"X-API-Key": key}, json={"model": "m1", "data": 1})
    assert r.json() == {"detail": "values must not be empty"}


async def test_an_operator_can_choose_to_pass_model_errors_through(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url)
    key = await store_key(redis)
    upstream.status, upstream.payload = 500, {"error": LEAK}
    async with await make_client(redis, http_session, expose_upstream_errors=True) as client:
        r = await client.post("/predict", headers={"X-API-Key": key}, json={"model": "m1", "data": 1})
    assert LEAK in r.text


async def test_a_failed_jobs_error_text_is_replaced(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url, mode="queue")
    key = await store_key(redis)
    await redis.set(f"jobowner:m1~j1", key.split("_")[1])
    upstream.job_get_payload = {
        "job_id": "j1", "status": "failed", "result": None, "error": LEAK, "error_type": "KeyError", "attempts": 1,
    }
    async with await make_client(redis, http_session) as client:
        r = await client.get("/jobs/m1~j1", headers={"X-API-Key": key})
    body = r.json()
    assert body["status"] == "failed" and body["job_id"] == "m1~j1"
    assert "weights" not in r.text
    assert body["error"] == "The job failed"
    assert body["error_type"] == "KeyError"  # the kind of failure is useful and says nothing about internals


async def test_succeeded_jobs_are_not_touched(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url, mode="queue")
    key = await store_key(redis)
    await redis.set("jobowner:m1~j1", key.split("_")[1])
    upstream.job_get_payload = {"job_id": "j1", "status": "succeeded", "result": {"score": 1}, "error": None}
    async with await make_client(redis, http_session) as client:
        r = await client.get("/jobs/m1~j1", headers={"X-API-Key": key})
    assert r.json() == {"job_id": "m1~j1", "status": "succeeded", "result": {"score": 1}, "error": None}


# ── the API description is not public ──


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_interactive_docs_are_off_by_default(redis, http_session, path):
    async with await make_client(redis, http_session) as client:
        assert (await client.get(path)).status_code == 404


async def test_docs_can_be_turned_on(redis, http_session):
    async with await make_client(redis, http_session, enable_docs=True) as client:
        assert (await client.get("/openapi.json")).status_code == 200


# ── /health does not become a Redis amplifier ──


class CountingRedis:
    def __init__(self, inner):
        self._inner = inner
        self.pings = 0
        self.scans = 0
        self.gets = 0

    async def ping(self):
        self.pings += 1
        return await self._inner.ping()

    def scan_iter(self, *a, **k):
        self.scans += 1
        return self._inner.scan_iter(*a, **k)

    async def get(self, *a, **k):
        self.gets += 1
        return await self._inner.get(*a, **k)

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def test_health_pings_redis_at_most_once_per_cache_window(redis, http_session):
    counting = CountingRedis(redis)
    async with await make_client(counting, http_session, health_cache_seconds=60) as client:
        for _ in range(5):
            assert (await client.get("/health")).json() == {"status": "ok"}
    assert counting.pings == 1


async def test_health_cache_can_be_turned_off(redis, http_session):
    counting = CountingRedis(redis)
    async with await make_client(counting, http_session, health_cache_seconds=0) as client:
        for _ in range(3):
            await client.get("/health")
    assert counting.pings == 3


# ── /models reads an index instead of scanning the keyspace ──


async def test_catalog_uses_the_route_index_when_the_supervisor_maintains_one(redis, http_session):
    await store_route(redis, "ecg-1")
    await store_route(redis, "ecg-2")
    await redis.set("route_index_ready", "1")
    await redis.sadd("route_index", "ecg-1", "ecg-2", "gone")  # "gone" has no route record any more
    key = await store_key(redis, allow_all=True)
    counting = CountingRedis(redis)
    async with await make_client(counting, http_session) as client:
        r = await client.get("/models", headers={"X-API-Key": key})
    assert [m["name"] for m in r.json()["models"]] == ["ecg-1", "ecg-2"]
    assert counting.scans == 0


async def test_catalog_falls_back_to_scanning_until_an_index_exists(redis, http_session):
    await store_route(redis, "ecg-1")
    key = await store_key(redis, allow_all=True)
    counting = CountingRedis(redis)
    async with await make_client(counting, http_session) as client:
        r = await client.get("/models", headers={"X-API-Key": key})
    assert [m["name"] for m in r.json()["models"]] == ["ecg-1"]
    assert counting.scans == 1


async def test_catalog_reads_each_models_records_in_bulk(redis, http_session):
    for i in range(10):
        await store_route(redis, f"m{i}")
        await redis.sadd("route_index", f"m{i}")
    await redis.set("route_index_ready", "1")
    key = await store_key(redis, allow_all=True)
    counting = CountingRedis(redis)
    async with await make_client(counting, http_session) as client:
        r = await client.get("/models", headers={"X-API-Key": key})
    assert len(r.json()["models"]) == 10
    assert counting.gets <= 2  # the key record, and nothing per model


async def test_a_big_failed_jobs_error_text_is_replaced_too(redis, http_session, upstream):
    # replies over 1 MiB are not parsed, but they must not be a way round the error sanitizing
    await store_route(redis, "m1", url=upstream.url, mode="queue")
    key = await store_key(redis)
    await redis.set("jobowner:m1~j1", key.split("_")[1])
    upstream.job_get_body = (
        b'{"job_id":"j1","status":"failed","result":null,"error":"' + LEAK.encode() + b'","padding":"'
        + b"x" * (1024 * 1024 + 10) + b'"}'
    )
    async with await make_client(redis, http_session) as client:
        r = await client.get("/jobs/m1~j1", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert "weights" not in r.text
    body = r.json()
    assert (body["job_id"], body["status"], body["error"]) == ("m1~j1", "failed", "The job failed")
    assert len(r.content) < 1024


async def test_a_big_successful_job_result_is_still_passed_on_whole(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url, mode="queue")
    key = await store_key(redis)
    await redis.set("jobowner:m1~j1", key.split("_")[1])
    upstream.job_get_body = b'{"job_id":"j1","status":"succeeded","result":{"v":"' + b"x" * (1024 * 1024 + 10) + b'"}}'
    async with await make_client(redis, http_session) as client:
        r = await client.get("/jobs/m1~j1", headers={"X-API-Key": key})
    assert r.json()["job_id"] == "m1~j1"
    assert len(r.json()["result"]["v"]) == 1024 * 1024 + 10
