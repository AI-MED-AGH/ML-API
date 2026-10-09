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


# ───────────── hostile upstreams ─────────────
async def test_redirects_are_never_followed(upstream, http_session):
    from tests.upstream import FakeUpstream

    target = FakeUpstream()
    await target.start()
    try:
        upstream.redirect_to = f"{target.url}/predict"
        with pytest.raises(UpstreamError):
            await forward(http_session, upstream.url, {"data": 1}, **KW)
        assert target.requests == []                     # the redirect target was never contacted
    finally:
        await target.stop()


async def raw(response: bytes):
    from tests.upstream import RawUpstream

    server = RawUpstream(response)
    await server.start()
    return server


@pytest.mark.parametrize("status_line", ["HTTP/1.1 999 Weird", "HTTP/1.1 600 Weird", "HTTP/1.1 100 Continue"])
async def test_out_of_range_status_codes_are_an_upstream_error(http_session, status_line):
    server = await raw(f"{status_line}\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{{}}".encode())
    try:
        with pytest.raises(UpstreamError):
            await forward(http_session, server.url, {"data": 1}, **KW)
    finally:
        await server.stop()


@pytest.mark.parametrize(
    "retry_after,expected",
    [(b"30", "30"), (b"0", "0"), (b"", None), (b"soon", None), (b"-5", None), (b"1" * 11, None),
     ("€".encode(), None), (b"\xe9", None), (b"Wed, 21 Oct 2026 07:28:00 GMT", None), (b"a" * 8000, None)],
)
async def test_retry_after_is_forwarded_only_when_it_is_a_small_number(http_session, retry_after, expected):
    server = await raw(b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: " + retry_after + b"\r\nContent-Length: 2\r\n\r\n{}")
    try:
        result = await forward(http_session, server.url, {"data": 1}, **KW)
    finally:
        await server.stop()
    assert result.status == 503 and result.retry_after == expected


async def test_waiting_for_a_pooled_connection_is_not_a_connect_timeout(upstream):
    import asyncio

    import aiohttp

    upstream.delay = 0.4
    connector = aiohttp.TCPConnector(limit_per_host=1)
    async with aiohttp.ClientSession(connector=connector) as session:
        calls = [forward(session, upstream.url, {"data": i}, total_timeout=10, connect_timeout=0.2) for i in range(3)]
        results = await asyncio.gather(*calls)
    assert [r.status for r in results] == [200, 200, 200]      # queued requests wait; they do not fail after 0.2 s


async def test_malformed_upstream_replies_do_not_leak_their_bytes_into_logs(http_session, caplog):
    server = await raw(b"SECRET-PAYLOAD-123\r\n\r\nmore secret bytes")
    try:
        with caplog.at_level("DEBUG"):
            with pytest.raises(UpstreamError):
                await forward(http_session, server.url, {"data": 1}, **KW)
    finally:
        await server.stop()
    assert "SECRET" not in caplog.text and "secret bytes" not in caplog.text
