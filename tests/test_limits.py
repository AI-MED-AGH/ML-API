import asyncio

import httpx
import pytest

from src.config import Settings
from src.limits import parse_limits
from src.main import create_app
from tests.helpers import store_key, store_route

KEY_FIELDS = ("rpm", "poll_rpm", "jobs_per_day", "max_outstanding_jobs")


async def make_client(redis, http_session, **overrides):
    settings = Settings(redis_url="redis://x/0", _env_file=None, **overrides)
    app = create_app(settings, redis=redis, session=http_session)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://r")


def predict(client, key, model="m1"):
    return client.post("/predict", headers={"X-API-Key": key}, json={"model": model, "data": 1})


# ── parsing ──


def test_parse_limits_keeps_only_known_non_negative_integers():
    raw = {"rpm": 5, "poll_rpm": 0, "jobs_per_day": -1, "max_outstanding_jobs": "9", "bogus": 3, "extra": True}
    assert parse_limits(raw, KEY_FIELDS) == {"rpm": 5, "poll_rpm": 0}


@pytest.mark.parametrize("raw", [None, "x", [], 7])
def test_parse_limits_ignores_non_objects(raw):
    assert parse_limits(raw, KEY_FIELDS) == {}


def test_parse_limits_rejects_booleans_and_floats():
    assert parse_limits({"rpm": True, "poll_rpm": 1.5}, KEY_FIELDS) == {}


# ── per-key limits set by an admin override the platform defaults ──


async def test_a_keys_own_limit_replaces_the_default(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url)
    tight = await store_key(redis, limits={"rpm": 1})
    async with await make_client(redis, http_session) as client:
        assert (await predict(client, tight)).status_code == 200
        assert (await predict(client, tight)).status_code == 429


async def test_a_key_can_be_allowed_more_than_the_default(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url)
    generous = await store_key(redis, limits={"rpm": 5})
    async with await make_client(redis, http_session, rate_limit_per_minute=1) as client:
        codes = [(await predict(client, generous)).status_code for _ in range(5)]
    assert codes == [200] * 5


async def test_zero_on_a_key_means_unlimited(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url)
    key = await store_key(redis, limits={"rpm": 0})
    async with await make_client(redis, http_session, rate_limit_per_minute=1) as client:
        codes = [(await predict(client, key)).status_code for _ in range(4)]
    assert codes == [200] * 4


async def test_malformed_key_limits_fall_back_to_the_default(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url)
    key = await store_key(redis, limits={"rpm": "lots", "poll_rpm": -4})
    async with await make_client(redis, http_session, rate_limit_per_minute=1) as client:
        assert (await predict(client, key)).status_code == 200
        assert (await predict(client, key)).status_code == 429


# ── per-model limits published with the route ──


async def test_model_limit_per_key_is_counted_for_each_key_separately(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url, limits={"rpm_per_key": 1})
    a, b = await store_key(redis), await store_key(redis)
    async with await make_client(redis, http_session) as client:
        assert (await predict(client, a)).status_code == 200
        blocked = await predict(client, a)
        assert (await predict(client, b)).status_code == 200
    assert blocked.status_code == 429 and blocked.json()["error_type"] == "RateLimited"
    assert int(blocked.headers["Retry-After"]) >= 1


async def test_model_total_limit_is_shared_by_every_key(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url, limits={"rpm_total": 2})
    keys = [await store_key(redis) for _ in range(3)]
    async with await make_client(redis, http_session) as client:
        codes = [(await predict(client, k)).status_code for k in keys]
    assert codes == [200, 200, 429]
    assert len(upstream.requests) == 2


async def test_one_models_limits_do_not_touch_another_model(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url, limits={"rpm_total": 1})
    await store_route(redis, "m2", url=upstream.url)
    key = await store_key(redis, allowed_models=("m1", "m2"))
    async with await make_client(redis, http_session) as client:
        assert (await predict(client, key, "m1")).status_code == 200
        assert (await predict(client, key, "m1")).status_code == 429
        assert (await predict(client, key, "m2")).status_code == 200


async def test_unreachable_models_do_not_spend_model_budget(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url, limits={"rpm_total": 1})
    outsider = await store_key(redis, allowed_models=("other",))
    insider = await store_key(redis)
    async with await make_client(redis, http_session) as client:
        for _ in range(3):
            assert (await predict(client, outsider)).status_code == 404
        assert (await predict(client, insider)).status_code == 200


async def test_model_limits_also_apply_to_job_submission(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url, mode="queue", limits={"rpm_per_key": 1})
    key = await store_key(redis)
    upstream.job_status = 202
    async with await make_client(redis, http_session) as client:
        send = lambda: client.post("/jobs", headers={"X-API-Key": key}, json={"model": "m1", "data": 1})  # noqa: E731
        upstream.job_payload = {"job_id": "a1"}
        assert (await send()).status_code == 202
        assert (await send()).status_code == 429


# ── concurrency ──


async def test_concurrency_limit_refuses_instead_of_queueing(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url, limits={"max_concurrency": 1})
    upstream.delay = 0.3
    a, b = await store_key(redis), await store_key(redis)
    async with await make_client(redis, http_session) as client:
        first = asyncio.create_task(predict(client, a))
        await asyncio.sleep(0.1)
        refused = await predict(client, b)
        assert (await first).status_code == 200
        upstream.delay = 0
        assert (await predict(client, b)).status_code == 200  # the slot was given back
    assert refused.status_code == 429
    assert refused.json()["error_type"] == "ModelBusy"
    assert int(refused.headers["Retry-After"]) >= 1


async def test_slot_is_given_back_when_the_model_fails(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url, limits={"max_concurrency": 1})
    key = await store_key(redis)
    upstream.body = b"<html>"
    async with await make_client(redis, http_session) as client:
        assert (await predict(client, key)).status_code == 502
        upstream.body = None
        assert (await predict(client, key)).status_code == 200


async def test_default_concurrency_applies_when_the_model_sets_none(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url)
    upstream.delay = 0.3
    keys = [await store_key(redis) for _ in range(3)]
    async with await make_client(redis, http_session, model_max_concurrency=1) as client:
        tasks = [asyncio.create_task(predict(client, k)) for k in keys]
        codes = sorted(r.status_code for r in await asyncio.gather(*tasks))
    assert codes == [200, 429, 429]
