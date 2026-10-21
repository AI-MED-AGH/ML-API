import itertools
import time

import httpx
import pytest

from src.config import Settings
from src.main import create_app
from tests.helpers import store_key, store_route


async def make_client(redis, http_session, **overrides):
    settings = Settings(redis_url="redis://x/0", _env_file=None, rate_limit_per_minute=0, **overrides)
    app = create_app(settings, redis=redis, session=http_session)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://r")


_ids = itertools.count(1)  # job ids are unique across keys, as a real model would hand them out


class Jobs:
    """Submits jobs for one key against a fake model that hands out a new id each time."""

    def __init__(self, client, upstream, key):
        self.client, self.upstream, self.key = client, upstream, key

    async def submit(self):
        self.upstream.job_payload = {"job_id": f"job-{next(_ids)}"}
        return await self.client.post(
            "/jobs", headers={"X-API-Key": self.key}, json={"model": "m1", "data": 1}
        )

    async def poll(self, public_id, status):
        self.upstream.job_get_payload = {"job_id": public_id.split("~")[1], "status": status}
        return await self.client.get(f"/jobs/{public_id}", headers={"X-API-Key": self.key})


@pytest.fixture
async def setup(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url, mode="queue")


async def test_a_key_cannot_have_more_unfinished_jobs_than_the_cap(setup, redis, http_session, upstream):
    key = await store_key(redis)
    async with await make_client(redis, http_session, max_outstanding_jobs=2) as client:
        jobs = Jobs(client, upstream, key)
        assert (await jobs.submit()).status_code == 202
        assert (await jobs.submit()).status_code == 202
        refused = await jobs.submit()
    assert refused.status_code == 429
    assert refused.json()["error_type"] == "RateLimited"
    assert int(refused.headers["Retry-After"]) >= 1
    assert len(upstream.job_requests) == 2  # the refused job never reached the model


async def test_another_key_has_its_own_allowance(setup, redis, http_session, upstream):
    a, b = await store_key(redis), await store_key(redis)
    async with await make_client(redis, http_session, max_outstanding_jobs=1) as client:
        assert (await Jobs(client, upstream, a).submit()).status_code == 202
        assert (await Jobs(client, upstream, b).submit()).status_code == 202


@pytest.mark.parametrize("final", ["succeeded", "failed"])
async def test_seeing_a_job_finish_frees_its_slot(setup, redis, http_session, upstream, final):
    key = await store_key(redis)
    async with await make_client(redis, http_session, max_outstanding_jobs=1) as client:
        jobs = Jobs(client, upstream, key)
        first = (await jobs.submit()).json()["job_id"]
        assert (await jobs.submit()).status_code == 429
        assert (await jobs.poll(first, "running")).status_code == 200
        assert (await jobs.submit()).status_code == 429  # still unfinished
        assert (await jobs.poll(first, final)).status_code == 200
        assert (await jobs.submit()).status_code == 202


async def test_the_polled_body_is_unchanged_by_the_bookkeeping(setup, redis, http_session, upstream):
    key = await store_key(redis)
    async with await make_client(redis, http_session) as client:
        jobs = Jobs(client, upstream, key)
        public = (await jobs.submit()).json()["job_id"]
        done = await jobs.poll(public, "succeeded")
    assert done.json() == {"job_id": public, "status": "succeeded"}


async def test_a_job_the_model_refused_does_not_use_a_slot(setup, redis, http_session, upstream):
    key = await store_key(redis)
    async with await make_client(redis, http_session, max_outstanding_jobs=1) as client:
        jobs = Jobs(client, upstream, key)
        upstream.job_status = 503
        assert (await jobs.submit()).status_code == 503
        upstream.job_status = 202
        assert (await jobs.submit()).status_code == 202


async def test_old_entries_expire_on_their_own(setup, redis, http_session, upstream):
    key = await store_key(redis)
    key_id = key.split("_")[1]
    await redis.zadd(f"outstanding:{key_id}", {"m1~long-gone": time.time() - 5})
    async with await make_client(redis, http_session, max_outstanding_jobs=1) as client:
        assert (await Jobs(client, upstream, key).submit()).status_code == 202


async def test_a_keys_own_cap_wins_and_zero_is_unlimited(setup, redis, http_session, upstream):
    tight = await store_key(redis, limits={"max_outstanding_jobs": 1})
    open_ended = await store_key(redis, limits={"max_outstanding_jobs": 0})
    async with await make_client(redis, http_session, max_outstanding_jobs=5) as client:
        t = Jobs(client, upstream, tight)
        assert (await t.submit()).status_code == 202
        assert (await t.submit()).status_code == 429
        o = Jobs(client, upstream, open_ended)
        codes = [(await o.submit()).status_code for _ in range(8)]
    assert codes == [202] * 8


async def test_large_job_results_are_passed_on_without_a_full_parse(setup, redis, http_session, upstream):
    key = await store_key(redis)
    big = '"' + "x" * (1024 * 1024 + 10) + '"'
    async with await make_client(redis, http_session, max_outstanding_jobs=1) as client:
        jobs = Jobs(client, upstream, key)
        public = (await jobs.submit()).json()["job_id"]
        job_id = public.split("~")[1]
        upstream.job_get_body = f'{{"job_id":"{job_id}","status":"succeeded","result":{big}}}'.encode()
        r = await client.get(f"/jobs/{public}", headers={"X-API-Key": key})
        assert r.status_code == 200
        assert r.content.startswith(f'{{"job_id":"{public}","status":"succeeded"'.encode())  # id rewritten, tail untouched
        assert r.content.endswith(big.encode() + b"}")
        assert (await jobs.submit()).status_code == 202  # the slot was freed
