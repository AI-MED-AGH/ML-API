import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from src.common.errors import RedisUnavailable
from src.routing.activity import ActivityTracker
from src.routing.routes import RouteStore
from src.routing.wake import WakeRequester
from tests.helpers import store_route


async def test_get_existing_route(redis):
    await store_route(redis, "m1", state="sleeping", mode="queue")
    route = await RouteStore(redis).get("m1")
    assert route.state == "sleeping"
    assert route.mode == "queue"
    assert route.url.startswith("http://")


async def test_mode_defaults_to_sync(redis):
    await redis.set("route:m1", '{"url": "http://x", "state": "ready"}')
    assert (await RouteStore(redis).get("m1")).mode == "sync"


async def test_get_missing_route_is_none(redis):
    assert await RouteStore(redis).get("nope") is None


@pytest.mark.parametrize(
    "value",
    ["{bad", "[]", '{"state": "ready"}', '{"url": 1, "state": "ready"}',
     '{"url": "http://x", "state": "ready", "mode": "weird"}'],
)
async def test_malformed_route_is_none(redis, value):
    await redis.set("route:m1", value)
    assert await RouteStore(redis).get("m1") is None


async def test_list_names_sorted(redis):
    for name in ("b", "a", "c"):
        await store_route(redis, name)
    await redis.set("key:abc", "x")  # unrelated keys are ignored
    assert await RouteStore(redis).list_names() == ["a", "b", "c"]


class BrokenRedis:
    async def get(self, *_a, **_k):
        raise RedisConnectionError("down")

    def scan_iter(self, *_a, **_k):
        raise RedisConnectionError("down")

    async def set(self, *_a, **_k):
        raise RedisConnectionError("down")

    async def lpush(self, *_a, **_k):
        raise RedisConnectionError("down")


async def test_route_store_redis_failure():
    with pytest.raises(RedisUnavailable):
        await RouteStore(BrokenRedis()).get("m1")
    with pytest.raises(RedisUnavailable):
        await RouteStore(BrokenRedis()).list_names()


async def test_activity_is_throttled(redis):
    now = [1000.0]
    tracker = ActivityTracker(redis, throttle_seconds=10, clock=lambda: now[0])
    await tracker.touch("m1")
    assert await redis.get("last_active:m1") == "1000"
    now[0] = 1005.0
    await tracker.touch("m1")
    assert await redis.get("last_active:m1") == "1000"  # throttled
    await redis.delete("activity_throttle:m1")  # simulate TTL expiry
    now[0] = 1011.0
    await tracker.touch("m1")
    assert await redis.get("last_active:m1") == "1011"


async def test_activity_never_raises():
    await ActivityTracker(BrokenRedis(), throttle_seconds=10).touch("m1")


async def test_wake_enqueues_once_per_window(redis):
    waker = WakeRequester(redis, pending_ttl_seconds=60)
    assert await waker.request("m1") is True
    assert await waker.request("m1") is False
    assert await redis.lrange("wake", 0, -1) == ["m1"]


async def test_wake_never_raises():
    assert await WakeRequester(BrokenRedis(), pending_ttl_seconds=60).request("m1") is False


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://host/x", "gopher://h", "http://", "//host/x", "host:8000"]
)
async def test_route_with_non_http_url_is_rejected(redis, url):
    import json

    await redis.set("route:m1", json.dumps({"url": url, "state": "ready", "mode": "sync"}))
    assert await RouteStore(redis).get("m1") is None
