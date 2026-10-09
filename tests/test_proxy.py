import json

import pytest

from src.common.errors import UpstreamError, UpstreamTimeout
from src.routing.proxy import forward

KW = dict(total_timeout=5.0, connect_timeout=1.0)


async def test_forwards_payload_and_returns_status_and_body(upstream, http_session):
    upstream.payload = {"success": True, "prediction": 1}
    result = await forward(
        http_session, upstream.url, {"data": {"x": 1}, "metadata": {"a": 1}}, **KW
    )
    assert result.status == 200
    assert json.loads(result.body) == {"success": True, "prediction": 1}
    assert upstream.requests == [{"data": {"x": 1}, "metadata": {"a": 1}}]


async def test_passes_through_error_status(upstream, http_session):
    upstream.status, upstream.payload = 422, {"error": "bad input"}
    result = await forward(http_session, upstream.url, {"data": 1}, **KW)
    assert result.status == 422
    assert json.loads(result.body) == {"error": "bad input"}


async def test_json_array_body_is_passed_through(upstream, http_session):
    upstream.payload = [1, 2, 3]
    result = await forward(http_session, upstream.url, {"data": 1}, **KW)
    assert json.loads(result.body) == [1, 2, 3]


@pytest.mark.parametrize("body", [b"not json", b"", b"<html>502</html>"])
async def test_non_json_body_is_upstream_error(upstream, http_session, body):
    upstream.body = body
    with pytest.raises(UpstreamError):
        await forward(http_session, upstream.url, {"data": 1}, **KW)


async def test_timeout_maps_to_upstream_timeout(upstream, http_session):
    upstream.delay = 1.0
    with pytest.raises(UpstreamTimeout):
        await forward(
            http_session, upstream.url, {"data": 1}, total_timeout=0.1, connect_timeout=0.1
        )


async def test_connection_refused_maps_to_upstream_error(http_session):
    with pytest.raises(UpstreamError):
        await forward(http_session, "http://127.0.0.1:1", {"data": 1}, **KW)


async def test_trailing_slash_in_base_url(upstream, http_session):
    assert (await forward(http_session, upstream.url + "/", {"data": 1}, **KW)).status == 200


async def test_oversized_upstream_response_is_rejected(upstream, http_session):
    upstream.body = b'{"x": "' + b"a" * 5000 + b'"}'
    with pytest.raises(UpstreamError):
        await forward(
            http_session, upstream.url, {"data": 1}, max_response_bytes=1000, **KW
        )


async def test_body_is_passed_through_byte_for_byte_even_with_nan(upstream, http_session):
    upstream.body = b'{"score":   NaN, "a":1}'
    result = await forward(http_session, upstream.url, {"data": 1}, **KW)
    assert result.body == b'{"score":   NaN, "a":1}'


async def test_retry_after_is_captured(upstream, http_session):
    upstream.status = 503
    upstream.headers = {"Retry-After": "30"}
    result = await forward(http_session, upstream.url, {"data": 1}, **KW)
    assert result.status == 503
    assert result.retry_after == "30"
