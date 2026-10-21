import asyncio
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


async def test_a_reply_stays_counted_while_the_client_is_not_reading(redis, http_session, upstream):
    """The outermost `send` is the server writing to the client; it does not return while the client is not reading."""
    await store_route(redis, "m1", url=upstream.url)
    key = await store_key(redis)
    upstream.body = b'{"v":"' + b"x" * 5000 + b'"}'
    app = create_app(Settings(redis_url="redis://x/0", _env_file=None), redis=redis, session=http_session)
    client_reads = asyncio.Event()
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": json.dumps({"model": "m1", "data": 1}).encode(), "more_body": False}
        await asyncio.sleep(3600)

    async def send(message):
        if message["type"] == "http.response.body":
            await client_reads.wait()

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "path": "/predict",
        "raw_path": b"/predict", "query_string": b"", "scheme": "http", "server": ("r", 80), "client": ("c", 1),
        "root_path": "", "headers": [(b"x-api-key", key.encode()), (b"content-type", b"application/json")],
    }
    request = asyncio.create_task(app(scope, receive, send))
    await asyncio.sleep(0.3)
    held_while_stalled = app.state.budget.in_use
    client_reads.set()
    await request
    assert held_while_stalled >= len(upstream.body)  # a slow reader cannot hold memory the budget no longer knows about
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


# ── a big reply is written out in pieces, so that the server's own write buffer cannot absorb all of it at once ──


async def _run_held_response(body: bytes) -> list[dict]:
    from src.routing.proxy import HeldResponse

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.disconnect"}

    response = HeldResponse(content=body, media_type="application/json", release=lambda: None)
    await response({"type": "http", "asgi": {"version": "3.0"}}, receive, send)
    return messages


async def test_a_big_reply_is_sent_in_pieces_that_add_up_to_the_whole_body():
    body = b'{"v":"' + b"x" * 300_000 + b'"}'
    messages = await _run_held_response(body)
    start, parts = messages[0], messages[1:]
    assert start["type"] == "http.response.start"
    assert dict(start["headers"])[b"content-length"] == str(len(body)).encode()  # clients still see one plain reply
    assert len(parts) > 2
    assert all(m["more_body"] for m in parts[:-1]) and parts[-1]["more_body"] is False
    assert b"".join(m["body"] for m in parts) == body
    assert max(len(m["body"]) for m in parts) <= 64 * 1024


async def test_a_small_reply_is_still_one_message():
    messages = await _run_held_response(b'{"ok":true}')
    assert [m["type"] for m in messages] == ["http.response.start", "http.response.body"]
    assert messages[1]["body"] == b'{"ok":true}' and messages[1].get("more_body", False) is False


async def test_a_client_that_stops_reading_is_cut_off_and_the_budget_comes_back():
    from src.routing.proxy import HeldResponse

    budget = ResponseBudget(10_000_000)
    body = b'{"v":"' + b"x" * 300_000 + b'"}'
    budget.reserve(len(body))
    sent = []

    async def send(message):
        sent.append(message)
        if len(sent) >= 3:
            await asyncio.sleep(3600)  # the client's buffers are full and it reads nothing more

    async def receive():
        return {"type": "http.disconnect"}

    response = HeldResponse(
        content=body, media_type="application/json", release=lambda: budget.release(len(body)), send_timeout=0.05
    )
    await asyncio.wait_for(response({"type": "http", "asgi": {"version": "3.0"}}, receive, send), timeout=5)
    assert budget.in_use == 0
    assert len(sent) == 3  # it stopped there: the reply was never completed


async def test_a_send_timeout_of_zero_waits_as_long_as_the_client_needs():
    from src.routing.proxy import HeldResponse

    body = b'{"v":"' + b"x" * 300_000 + b'"}'
    sent = []

    async def send(message):
        await asyncio.sleep(0.01)
        sent.append(message)

    async def receive():
        return {"type": "http.disconnect"}

    response = HeldResponse(content=body, media_type="application/json", release=lambda: None, send_timeout=0)
    await response({"type": "http", "asgi": {"version": "3.0"}}, receive, send)
    assert sent[-1]["more_body"] is False


def test_the_default_send_deadline_is_a_couple_of_minutes():
    assert Settings(redis_url="redis://x/0", _env_file=None).response_send_timeout == 120
