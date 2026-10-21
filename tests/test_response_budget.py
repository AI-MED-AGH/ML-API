import json

import httpx
import pytest

from src.common.errors import RouterBusy, UpstreamError
from src.config import Settings
from src.main import create_app
from src.routing.budget import ResponseBudget
from src.routing.proxy import FULL_PARSE_LIMIT, forward
from tests.helpers import store_key, store_route

KW = dict(total_timeout=5.0, connect_timeout=1.0)


def test_budget_grants_until_full_then_refuses_and_frees_on_release():
    budget = ResponseBudget(1000)
    assert budget.reserve(600) is True
    assert budget.reserve(600) is False  # would exceed: nothing is taken
    assert budget.in_use == 600
    budget.release(600)
    assert budget.reserve(600) is True


def test_budget_of_zero_is_unlimited():
    budget = ResponseBudget(0)
    assert budget.reserve(10**12) is True


async def test_response_over_the_router_budget_is_refused_and_nothing_stays_reserved(upstream, http_session):
    upstream.payload = {"blob": "x" * 5000}
    budget = ResponseBudget(1000)
    with pytest.raises(RouterBusy):
        await forward(http_session, upstream.url, {"data": 1}, budget=budget, **KW)
    assert budget.in_use == 0


async def test_a_successful_response_stays_counted_until_it_is_released(upstream, http_session):
    budget = ResponseBudget(100_000)
    result = await forward(http_session, upstream.url, {"data": 1}, budget=budget, **KW)
    assert budget.in_use == len(result.body)
    result.release()
    result.release()  # releasing twice must not free someone else's bytes
    assert budget.in_use == 0


async def test_a_replaced_5xx_body_is_not_counted(upstream, http_session):
    upstream.status, upstream.payload = 500, {"error": "x" * 5000}
    budget = ResponseBudget(100_000)
    await forward(http_session, upstream.url, {"data": 1}, budget=budget, expose_errors=False, **KW)
    assert budget.in_use == 0


async def test_budget_is_released_when_the_response_is_not_json(upstream, http_session):
    upstream.body = b"<html>" + b"x" * 500
    budget = ResponseBudget(100_000)
    with pytest.raises(UpstreamError):
        await forward(http_session, upstream.url, {"data": 1}, budget=budget, **KW)
    assert budget.in_use == 0


async def test_predict_answers_503_with_retry_after_when_the_router_is_full(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url)
    key = await store_key(redis)
    settings = Settings(redis_url="redis://x/0", _env_file=None, max_inflight_response_bytes=1000)
    app = create_app(settings, redis=redis, session=http_session)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://r") as client:
        headers, body = {"X-API-Key": key}, {"model": "m1", "data": 1}
        upstream.payload = {"blob": "x" * 5000}
        busy = await client.post("/predict", headers=headers, json=body)
        upstream.payload = {"ok": True}
        after = await client.post("/predict", headers=headers, json=body)
    assert busy.status_code == 503
    assert busy.json()["error_type"] == "RouterBusy"
    assert int(busy.headers["Retry-After"]) >= 1
    assert after.status_code == 200  # the refused response did not leave bytes reserved


def _big(inner: bytes) -> bytes:
    return b'{"a":"' + inner + b'"}'


async def test_large_valid_json_passes_through_unchanged(upstream, http_session):
    upstream.body = _big(b"x" * (FULL_PARSE_LIMIT + 10))
    result = await forward(http_session, upstream.url, {"data": 1}, **KW)
    assert result.body == upstream.body


@pytest.mark.parametrize(
    "body",
    [
        b"<html>" + b"x" * (FULL_PARSE_LIMIT + 10) + b"</html>",  # an error page
        b'{"a": 1' + b" " * (FULL_PARSE_LIMIT + 10),  # cut off before the closing brace
        b"[" + b"1," * 10 + b" " * (FULL_PARSE_LIMIT + 10) + b"}",  # opens like an array, ends like an object
    ],
)
async def test_large_non_json_bodies_are_still_rejected(upstream, http_session, body):
    upstream.body = body
    with pytest.raises(UpstreamError):
        await forward(http_session, upstream.url, {"data": 1}, **KW)


async def test_small_bodies_are_still_fully_validated(upstream, http_session):
    upstream.body = b'{"a": nope}'
    with pytest.raises(UpstreamError):
        await forward(http_session, upstream.url, {"data": 1}, **KW)


async def test_large_bodies_are_only_shape_checked(upstream, http_session):
    # Documented trade-off: parsing megabytes of JSON stalls every other request on the replica (the parser holds the GIL).
    # The client parses its own copy anyway; a model that sends garbage inside well-formed braces only hurts its own callers.
    upstream.body = b'{"a": nope ' + b"x" * (FULL_PARSE_LIMIT + 10) + b"}"
    result = await forward(http_session, upstream.url, {"data": 1}, **KW)
    assert result.body == upstream.body
    assert json.dumps(result.status)  # reached the caller as a normal response


# ── the budget covers a reply until it has been handed to the server, not just until it was read from the model ──


async def _asgi_get_body_sizes(app, method, path, headers, body):
    """Calls the app directly and records how many reply bytes were counted at the moment each message was sent."""
    seen: list[int] = []
    sent = iter([{"type": "http.request", "body": body, "more_body": False}])

    async def receive():
        try:
            return next(sent)
        except StopIteration:
            return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            seen.append(app.state.budget.in_use)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path,
        "raw_path": path.encode(), "query_string": b"", "scheme": "http", "server": ("r", 80), "client": ("c", 1),
        "root_path": "", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    await app(scope, receive, send)
    return seen


async def test_a_reply_stays_counted_while_it_is_being_sent(redis, http_session, upstream):
    await store_route(redis, "m1", url=upstream.url)
    key = await store_key(redis)
    upstream.body = b'{"v":"' + b"x" * 5000 + b'"}'
    app = create_app(Settings(redis_url="redis://x/0", _env_file=None), redis=redis, session=http_session)
    seen = await _asgi_get_body_sizes(
        app, "POST", "/predict", {"X-API-Key": key, "Content-Type": "application/json"},
        json.dumps({"model": "m1", "data": 1}).encode(),
    )
    assert max(seen) >= len(upstream.body)  # a slow reader cannot hold memory the budget no longer knows about
    assert app.state.budget.in_use == 0  # and it is given back afterwards


async def _requests_leave_nothing_reserved(redis, http_session, upstream, send):
    app = create_app(Settings(redis_url="redis://x/0", _env_file=None), redis=redis, session=http_session)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://r") as client:
        await send(client)
    return app.state.budget.in_use


async def test_nothing_stays_reserved_after_predict_job_submit_and_job_poll(redis, http_session, upstream):
    await store_route(redis, "sync1", url=upstream.url)
    await store_route(redis, "q1", url=upstream.url, mode="queue")
    key = await store_key(redis, allowed_models=("sync1", "q1"))
    headers = {"X-API-Key": key}
    upstream.job_get_payload = {"job_id": "abc-123", "status": "succeeded", "result": {"v": 1}}

    async def send(client):
        assert (await client.post("/predict", headers=headers, json={"model": "sync1", "data": 1})).status_code == 200
        submitted = await client.post("/jobs", headers=headers, json={"model": "q1", "data": 1})
        assert submitted.status_code == 202
        assert (await client.get(f"/jobs/{submitted.json()['job_id']}", headers=headers)).status_code == 200
        upstream.status = 500
        assert (await client.post("/predict", headers=headers, json={"model": "sync1", "data": 1})).status_code == 500

    assert await _requests_leave_nothing_reserved(redis, http_session, upstream, send) == 0


def test_a_response_that_is_never_sent_gives_its_reservation_back():
    import gc

    from src.routing.proxy import HeldResponse

    budget = ResponseBudget(1000)
    budget.reserve(400)
    HeldResponse(content=b"{}", release=lambda: budget.release(400))
    gc.collect()
    assert budget.in_use == 0
