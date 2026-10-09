import json

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from src.main import create_app
from tests.helpers import new_key, store_key, store_route


async def seed(redis, upstream=None, models=("m1",), mode="queue", **route_kw):
    key = await store_key(redis, allowed_models=models)
    if upstream is not None:
        route_kw.setdefault("url", upstream.url)
    await store_route(redis, "m1", mode=mode, **route_kw)
    return {"X-API-Key": key}


def key_id_of(headers):
    return headers["X-API-Key"].split("_")[1]


def make_client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://r")


async def submit(client, headers, **body):
    body.setdefault("model", "m1")
    body.setdefault("data", {"x": 1})
    return await client.post("/jobs", headers=headers, json=body)


# ───────────── POST /jobs ─────────────
async def test_submit_returns_prefixed_id_forwards_body_and_records_owner(client, redis, upstream):
    headers = await seed(redis, upstream)
    r = await submit(client, headers, metadata={"trace": "t"})
    assert r.status_code == 202
    assert r.json() == {"job_id": "m1~abc-123"}
    assert upstream.job_requests == [{"data": {"x": 1}, "metadata": {"trace": "t"}}]   # `model` is not forwarded
    assert await redis.get("jobowner:m1~abc-123") == key_id_of(headers)
    assert 0 < await redis.ttl("jobowner:m1~abc-123") <= 86400


async def test_sync_model_rejects_jobs_with_400(client, redis, upstream):
    headers = await seed(redis, upstream, mode="sync")
    r = await submit(client, headers)
    assert r.status_code == 400 and r.json()["error_type"] == "WrongMode"
    assert upstream.job_requests == []


async def test_model_side_errors_pass_through_and_record_nothing(client, redis, upstream):
    headers = await seed(redis, upstream)
    upstream.job_status, upstream.job_payload = 422, {"detail": "bad features"}
    r = await submit(client, headers)
    assert r.status_code == 422 and r.json() == {"detail": "bad features"}
    upstream.job_status, upstream.job_payload, upstream.job_headers = 503, {"error": "redis down"}, {"Retry-After": "7"}
    r = await submit(client, headers)
    assert r.status_code == 503 and r.headers["retry-after"] == "7"
    assert [k async for k in redis.scan_iter(match="jobowner:*")] == []


@pytest.mark.parametrize(
    "payload",
    [{}, {"job_id": None}, {"job_id": 5}, {"job_id": ""}, {"job_id": "../x"}, {"job_id": "a b"}, {"job_id": "a" * 65},
     {"job_id": "abc\n"}, {"job_id": "a~b"}, [1, 2], "text"],
)
async def test_invalid_job_id_from_model_is_a_502(client, redis, upstream, payload):
    headers = await seed(redis, upstream)
    upstream.job_payload = payload
    r = await submit(client, headers)
    assert r.status_code == 502 and r.json()["error_type"] == "UpstreamError"
    assert [k async for k in redis.scan_iter(match="jobowner:*")] == []


async def test_non_json_reply_is_a_502(client, redis, upstream):
    headers = await seed(redis, upstream)
    upstream.job_body = b"<html>oops</html>"
    r = await submit(client, headers)
    assert r.status_code == 502 and "oops" not in r.text


async def test_unreachable_and_slow_model(redis, http_session, settings, upstream):
    settings.upstream_timeout = 0.1
    upstream.delay = 1.0
    app = create_app(settings, redis=redis, session=http_session)
    headers = await seed(redis, upstream)
    async with make_client(app) as c:
        assert (await submit(c, headers)).status_code == 504
    headers = await seed(redis, url="http://127.0.0.1:1")
    async with make_client(app) as c:
        assert (await submit(c, headers)).status_code == 502


async def test_losing_the_ownership_record_fails_the_request(redis, http_session, settings, upstream):
    class FlakyWrites:
        def __init__(self, inner):
            self._inner = inner

        async def get(self, *a, **k):
            return await self._inner.get(*a, **k)

        def scan_iter(self, *a, **k):
            return self._inner.scan_iter(*a, **k)

        async def set(self, *a, **k):
            raise RedisConnectionError("write failed")

    headers = await seed(redis, upstream)
    app = create_app(settings, redis=FlakyWrites(redis), session=http_session)
    async with make_client(app) as c:
        r = await submit(c, headers)
    assert r.status_code == 503 and r.json()["error_type"] == "AuthBackendUnavailable"


async def test_auth_scope_and_unknown_model_behave_like_predict(client, redis, upstream):
    headers = await seed(redis, upstream)
    await store_route(redis, "secret-model", mode="queue", url=upstream.url)
    assert (await client.post("/jobs", json={"model": "m1", "data": 1})).status_code == 401
    r1 = await submit(client, headers, model="secret-model")           # exists, not in scope
    r2 = await submit(client, headers, model="does-not-exist")
    assert r1.status_code == r2.status_code == 404 and r1.text == r2.text
    assert upstream.job_requests == []


@pytest.mark.parametrize("state", ["starting", "unavailable", "waiting_for_gpu"])
async def test_model_that_is_not_ready_gives_503_without_waking(client, redis, upstream, state):
    headers = await seed(redis, upstream, state=state)
    r = await submit(client, headers)
    assert r.status_code == 503 and r.headers["x-model-status"] == state
    assert await redis.llen("wake") == 0 and upstream.job_requests == []


@pytest.mark.parametrize(
    "content",
    [b"not json", b"[1]", json.dumps({"model": "M1\n", "data": 1}).encode(), b"[" * 100_000],
)
async def test_hostile_bodies_are_422(client, redis, upstream, content):
    headers = await seed(redis, upstream)
    r = await client.post("/jobs", headers={**headers, "Content-Type": "application/json"}, content=content)
    assert r.status_code == 422
    assert upstream.job_requests == []


async def test_oversized_body_is_413(redis, http_session, settings, upstream):
    settings.max_body_bytes = 100
    app = create_app(settings, redis=redis, session=http_session)
    headers = await seed(redis, upstream)
    async with make_client(app) as c:
        assert (await submit(c, headers, data="x" * 500)).status_code == 413


# ───────────── GET /jobs/{id} ─────────────
async def submitted(client, redis, upstream):
    headers = await seed(redis, upstream)
    job_id = (await submit(client, headers)).json()["job_id"]
    return headers, job_id


async def test_owner_can_poll_and_job_id_is_rewritten(client, redis, upstream):
    headers, job_id = await submitted(client, redis, upstream)
    upstream.job_get_payload = {"job_id": "abc-123", "status": "succeeded", "result": {"class": 1}}
    r = await client.get(f"/jobs/{job_id}", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"job_id": "m1~abc-123", "status": "succeeded", "result": {"class": 1}}
    assert upstream.job_gets == ["abc-123"]                     # the model only ever sees its own id


async def test_model_status_codes_pass_through(client, redis, upstream):
    headers, job_id = await submitted(client, redis, upstream)
    upstream.job_get_status, upstream.job_get_payload = 404, {"detail": "job expired"}
    r = await client.get(f"/jobs/{job_id}", headers=headers)
    assert r.status_code == 404 and r.json() == {"detail": "job expired"}


async def test_bodies_without_a_job_id_and_nan_pass_through_unchanged(client, redis, upstream):
    headers, job_id = await submitted(client, redis, upstream)
    upstream.job_get_body = b'{"status": "failed",  "score": NaN}'
    r = await client.get(f"/jobs/{job_id}", headers=headers)
    assert r.status_code == 200 and r.content == b'{"status": "failed",  "score": NaN}'
    upstream.job_get_body = b"[1, 2]"
    assert (await client.get(f"/jobs/{job_id}", headers=headers)).content == b"[1, 2]"


async def test_other_keys_unknown_and_malformed_ids_are_one_identical_404(client, redis, upstream):
    headers, job_id = await submitted(client, redis, upstream)
    other = {"X-API-Key": await store_key(redis, allowed_models=("m1",))}       # valid key, not the owner
    texts = set()
    for h, jid in [
        (other, job_id), (headers, "m1~never-existed"), (headers, "nope"), (headers, "m1~"), (headers, "~abc-123"),
        (headers, "m1~../etc"), (headers, "m1~a b"), (headers, "M1~abc-123"),         (headers, "m1~" + "a" * 65), (headers, "m1~..%2Fsecrets"), (headers, "m1~%2e%2e"), (headers, "other~abc-123"),
    ]:
        r = await client.get(f"/jobs/{jid}", headers=h)
        assert r.status_code == 404, jid
        texts.add(r.text)
    assert len(texts) == 1
    assert upstream.job_gets == []                                               # nothing reached the model


async def test_polling_requires_a_key_and_respects_scope_changes(client, redis, upstream):
    headers, job_id = await submitted(client, redis, upstream)
    assert (await client.get(f"/jobs/{job_id}")).status_code == 401
    assert (await client.get(f"/jobs/{job_id}", headers={"X-API-Key": "junk"})).status_code == 401
    await redis.delete(f"key:{key_id_of(headers)}")                              # revoked
    assert (await client.get(f"/jobs/{job_id}", headers=headers)).status_code == 401


async def test_poll_after_the_key_lost_access_to_the_model_is_404(client, redis, upstream):
    headers, job_id = await submitted(client, redis, upstream)
    raw = json.loads(await redis.get(f"key:{key_id_of(headers)}"))
    raw["allowed_models"] = ["other"]
    await redis.set(f"key:{key_id_of(headers)}", json.dumps(raw))
    assert (await client.get(f"/jobs/{job_id}", headers=headers)).status_code == 404


async def test_polling_a_model_that_is_gone_or_down(client, redis, upstream):
    headers, job_id = await submitted(client, redis, upstream)
    await redis.delete("route:m1")
    assert (await client.get(f"/jobs/{job_id}", headers=headers)).status_code == 404
    await store_route(redis, "m1", mode="queue", url="http://127.0.0.1:1")
    r = await client.get(f"/jobs/{job_id}", headers=headers)
    assert r.status_code == 502 and r.json()["error_type"] == "UpstreamError"


async def test_polling_works_while_the_worker_sleeps_but_not_when_the_api_is_unavailable(client, redis, upstream):
    headers, job_id = await submitted(client, redis, upstream)
    await store_route(redis, "m1", mode="queue", url=upstream.url, state="unavailable")
    r = await client.get(f"/jobs/{job_id}", headers=headers)
    assert r.status_code == 503 and r.headers["x-model-status"] == "unavailable"


async def test_job_ownership_expires(client, redis, upstream):
    headers, job_id = await submitted(client, redis, upstream)
    await redis.delete(f"jobowner:{job_id}")                                     # TTL elapsed
    assert (await client.get(f"/jobs/{job_id}", headers=headers)).status_code == 404


async def _raw_get(app, path: str, key: str) -> int:
    """Calls the ASGI app with a path that really contains the characters (HTTP clients may normalise them away)."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode().replace(b"\n", b"%0A"), "query_string": b"", "root_path": "",
        "headers": [(b"x-api-key", key.encode())], "server": ("test", 80), "client": ("test", 1234),
    }
    await app(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


@pytest.mark.parametrize("suffix", ["\n", "\r", " ", "\t", "\x00", "é", "\u202e"])
async def test_ids_with_control_or_unicode_characters_are_404_and_never_reach_the_model(redis, http_session, settings, upstream, suffix):
    headers = await seed(redis, upstream)
    app = create_app(settings, redis=redis, session=http_session)
    assert await _raw_get(app, f"/jobs/m1~abc-123{suffix}", headers["X-API-Key"]) == 404
    assert await _raw_get(app, f"/jobs/m1{suffix}~abc-123", headers["X-API-Key"]) == 404
    assert upstream.job_gets == []


# ───────────── review findings ─────────────
async def test_a_model_cannot_hand_out_an_existing_job_id_to_a_second_key(client, redis, upstream):
    headers_a = await seed(redis, upstream)
    first = await submit(client, headers_a)
    assert first.status_code == 202
    headers_b = {"X-API-Key": await store_key(redis, allowed_models=("m1",))}
    second = await submit(client, headers_b)                       # the model answers with the same id again
    assert second.status_code == 502 and second.json()["error_type"] == "UpstreamError"
    assert await redis.get("jobowner:m1~abc-123") == key_id_of(headers_a)       # the owner did not change
    upstream.job_get_payload = {"job_id": "abc-123", "status": "succeeded", "result": "A-secret-result"}
    assert (await client.get("/jobs/m1~abc-123", headers=headers_b)).status_code == 404
    assert (await client.get("/jobs/m1~abc-123", headers=headers_a)).status_code == 200


async def test_the_same_key_resubmitting_an_id_is_fine(client, redis, upstream):
    headers = await seed(redis, upstream)
    assert (await submit(client, headers)).status_code == 202
    assert (await submit(client, headers)).status_code == 202


async def test_polling_extends_the_ownership_record(client, redis, upstream):
    headers, job_id = await submitted(client, redis, upstream)
    await redis.expire(f"jobowner:{job_id}", 30)
    assert (await client.get(f"/jobs/{job_id}", headers=headers)).status_code == 200
    assert await redis.ttl(f"jobowner:{job_id}") > 1000


async def test_job_endpoints_never_follow_redirects(client, redis, upstream):
    from tests.upstream import FakeUpstream

    target = FakeUpstream()
    await target.start()
    try:
        headers = await seed(redis, upstream)
        upstream.redirect_to = f"{target.url}/jobs"
        assert (await submit(client, headers)).status_code == 502
        upstream.redirect_to = None
        job_id = (await submit(client, headers)).json()["job_id"]
        upstream.redirect_to = f"{target.url}/jobs/x"
        assert (await client.get(f"/jobs/{job_id}", headers=headers)).status_code == 502
        assert target.requests == [] and target.job_requests == [] and target.job_gets == []
    finally:
        await target.stop()


async def test_typed_models_get_their_own_fields_through_jobs(client, redis, upstream):
    headers = await seed(redis, upstream)
    r = await client.post("/jobs", headers=headers, json={"model": "m1", "values": [1, 2]})
    assert r.status_code == 202 and upstream.job_requests == [{"values": [1, 2]}]
