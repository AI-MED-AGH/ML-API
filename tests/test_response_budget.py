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


async def test_budget_is_released_after_a_successful_response(upstream, http_session):
    budget = ResponseBudget(100_000)
    await forward(http_session, upstream.url, {"data": 1}, budget=budget, **KW)
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
