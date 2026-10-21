import httpx
from redis.exceptions import ConnectionError as RedisConnectionError

from src.config import Settings
from src.main import create_app
from src.ratelimit import RateLimiter
from tests.helpers import store_key, store_route


def limited_settings(**overrides) -> Settings:
    return Settings(redis_url="redis://localhost:6379/0", _env_file=None, **overrides)


async def make_client(redis, http_session, **overrides):
    app = create_app(limited_settings(**overrides), redis=redis, session=http_session)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://r")


async def test_limiter_allows_up_to_the_limit_then_reports_seconds_to_wait(redis):
    now = [1000.0]
    limiter = RateLimiter(redis, clock=lambda: now[0])
    for _ in range(3):
        assert await limiter.hit("predict", "k1", limit=3, window=60) is None
    retry_after = await limiter.hit("predict", "k1", limit=3, window=60)
    assert retry_after is not None and 1 <= retry_after <= 60


async def test_limiter_starts_a_fresh_count_in_the_next_window(redis):
    now = [1000.0]
    limiter = RateLimiter(redis, clock=lambda: now[0])
    assert await limiter.hit("predict", "k1", limit=1, window=60) is None
    assert await limiter.hit("predict", "k1", limit=1, window=60) is not None
    now[0] += 60
    assert await limiter.hit("predict", "k1", limit=1, window=60) is None


async def test_limiter_counts_each_key_and_scope_separately(redis):
    limiter = RateLimiter(redis)
    assert await limiter.hit("predict", "k1", limit=1, window=60) is None
    assert await limiter.hit("predict", "k2", limit=1, window=60) is None
    assert await limiter.hit("jobs", "k1", limit=1, window=60) is None


async def test_limit_zero_means_unlimited(redis):
    limiter = RateLimiter(redis)
    for _ in range(50):
        assert await limiter.hit("predict", "k1", limit=0, window=60) is None


async def test_predict_is_limited_per_key(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url)
    key_a = await store_key(redis)
    key_b = await store_key(redis)
    async with await make_client(redis, http_session, rate_limit_per_minute=2) as client:
        body = {"model": "m1", "data": 1}
        for _ in range(2):
            assert (await client.post("/predict", headers={"X-API-Key": key_a}, json=body)).status_code == 200
        blocked = await client.post("/predict", headers={"X-API-Key": key_a}, json=body)
        assert blocked.status_code == 429
        assert blocked.json()["error_type"] == "RateLimited"
        assert 1 <= int(blocked.headers["Retry-After"]) <= 60
        # another client is not affected by key_a's traffic
        assert (await client.post("/predict", headers={"X-API-Key": key_b}, json=body)).status_code == 200
    assert len(upstream.requests) == 3  # the blocked request never reached the model


async def test_rejected_request_does_not_read_the_body(redis, http_session):
    await store_route(redis, "m1")
    key = await store_key(redis)
    async with await make_client(redis, http_session, rate_limit_per_minute=1) as client:
        headers = {"X-API-Key": key}
        await client.post("/predict", headers=headers, content=b"not json")  # uses the only slot (422)
        r = await client.post("/predict", headers=headers, content=b"not json")
    assert r.status_code == 429  # not 422: limited before the body is parsed


async def test_unauthenticated_requests_do_not_consume_anyones_budget(redis, http_session):
    await store_route(redis, "m1")
    async with await make_client(redis, http_session, rate_limit_per_minute=1) as client:
        for _ in range(3):
            assert (await client.post("/predict", json={"model": "m1", "data": 1})).status_code == 401


async def test_job_submissions_have_a_daily_cap_per_key(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url, mode="queue")
    upstream.status, upstream.payload = 202, {"job_id": "abc"}
    key = await store_key(redis)
    async with await make_client(redis, http_session, job_submit_limit_per_day=1) as client:
        headers = {"X-API-Key": key}
        first = await client.post("/jobs", headers=headers, json={"model": "m1", "data": 1})
        second = await client.post("/jobs", headers=headers, json={"model": "m1", "data": 1})
    assert first.status_code == 202
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) > 60  # a daily window, not a per-minute one


async def test_polling_has_its_own_budget(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url, mode="queue")
    await redis.set("jobowner:m1~abc", "x")
    key = await store_key(redis)
    async with await make_client(
        redis, http_session, rate_limit_per_minute=1, job_poll_limit_per_minute=2
    ) as client:
        headers = {"X-API-Key": key}
        statuses = [(await client.get("/jobs/m1~abc", headers=headers)).status_code for _ in range(3)]
        submit = await client.post("/jobs", headers=headers, json={"model": "m1", "data": 1})
    assert statuses[-1] == 429
    assert statuses[:2] != [429, 429]
    assert submit.status_code != 429  # polling did not eat the submit budget


async def test_redis_failure_while_counting_fails_closed(redis, http_session):
    key = await store_key(redis)

    class FlakyRedis:
        def __init__(self, inner):
            self._inner = inner

        def pipeline(self, *a, **kw):
            raise RedisConnectionError("down")

        def __getattr__(self, name):
            return getattr(self._inner, name)

    async with await make_client(FlakyRedis(redis), http_session) as client:
        r = await client.post("/predict", headers={"X-API-Key": key}, json={"model": "m1", "data": 1})
    assert r.status_code == 503
