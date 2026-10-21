import logging
import time

import httpx

from src.config import Settings
from src.main import create_app
from tests.helpers import store_key, store_route


def today() -> str:
    return time.strftime("%Y%m%d", time.gmtime())


async def make_client(redis, http_session, **overrides):
    settings = Settings(redis_url="redis://x/0", _env_file=None, **overrides)
    app = create_app(settings, redis=redis, session=http_session)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://r")


async def usage_of(redis, key):
    return await redis.hgetall(f"usage:{today()}:{key.split('_')[1]}")


async def test_successful_requests_are_counted_per_key_and_model(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url)
    key = await store_key(redis)
    async with await make_client(redis, http_session) as client:
        for _ in range(2):
            await client.post("/predict", headers={"X-API-Key": key}, json={"model": "m1", "data": 1})
    usage = await usage_of(redis, key)
    assert usage["m1:requests"] == "2"
    assert "m1:errors" not in usage and "m1:limited" not in usage
    assert abs(int(usage["last_used"]) - time.time()) < 5
    assert 0 < await redis.ttl(f"usage:{today()}:{key.split('_')[1]}") <= 35 * 86400


async def test_failures_and_refusals_are_counted_apart(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url, limits={"rpm_per_key": 2})
    key = await store_key(redis)
    async with await make_client(redis, http_session) as client:
        call = lambda: client.post(  # noqa: E731
            "/predict", headers={"X-API-Key": key}, json={"model": "m1", "data": 1}
        )
        await call()
        upstream.body = b"<html>"
        await call()  # model fault -> 502
        await call()  # over the model's limit -> 429
    usage = await usage_of(redis, key)
    assert usage["m1:requests"] == "3"
    assert usage["m1:errors"] == "1"
    assert usage["m1:limited"] == "1"


async def test_requests_refused_before_the_model_is_known_are_still_counted(redis, http_session):
    key = await store_key(redis)
    async with await make_client(redis, http_session, rate_limit_per_minute=1) as client:
        for _ in range(2):
            await client.post("/predict", headers={"X-API-Key": key}, content=b"not json")
    usage = await usage_of(redis, key)
    assert usage["-:requests"] == "2"
    assert usage["-:limited"] == "1"


async def test_requests_without_a_valid_key_write_nothing(redis, http_session):
    async with await make_client(redis, http_session) as client:
        await client.post("/predict", json={"model": "m1", "data": 1})
        await client.post("/predict", headers={"X-API-Key": "mlapi_abcdefghijkl_" + "A" * 43}, json={})
    assert [k async for k in redis.scan_iter(match="usage:*")] == []


async def test_a_failing_counter_never_fails_the_request(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url)
    key = await store_key(redis)

    class NoUsageWrites:
        def __init__(self, inner):
            self._inner = inner

        def pipeline(self, *a, transaction=True, **k):
            pipe = self._inner.pipeline(*a, transaction=transaction, **k)
            real_hincrby = pipe.hincrby

            def hincrby(name, *args, **kw):
                if str(name).startswith("usage:"):
                    raise ConnectionError("usage store down")
                return real_hincrby(name, *args, **kw)

            pipe.hincrby = hincrby
            return pipe

        def __getattr__(self, name):
            return getattr(self._inner, name)

    settings = Settings(redis_url="redis://x/0", _env_file=None)
    app = create_app(settings, redis=NoUsageWrites(redis), session=http_session)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://r") as client:
        r = await client.post("/predict", headers={"X-API-Key": key}, json={"model": "m1", "data": 1})
    assert r.status_code == 200


async def test_each_request_is_logged_without_the_secret(redis, http_session, upstream, caplog):
    await store_route(redis, "m1", url=upstream.url)
    key = await store_key(redis)
    secret = key.split("_")[2]
    caplog.set_level(logging.INFO, logger="src.usage")
    async with await make_client(redis, http_session) as client:
        await client.post("/predict", headers={"X-API-Key": key}, json={"model": "m1", "data": 1})
    lines = [r.getMessage() for r in caplog.records if r.name == "src.usage"]
    assert len(lines) == 1
    assert f"key={key.split('_')[1]}" in lines[0] and "model=m1" in lines[0] and "status=200" in lines[0]
    assert secret not in caplog.text
