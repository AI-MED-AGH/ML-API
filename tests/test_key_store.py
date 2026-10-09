import json
import time

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from src.auth.store import KeyStore
from src.common.errors import RedisUnavailable, Unauthorized
from tests.helpers import new_key, store_key


async def test_valid_key_returns_context(redis):
    raw = await store_key(redis, allowed_models=("ecg-*", "xray"))
    ctx = await KeyStore(redis).authenticate(raw)
    assert ctx.allowed_models == ("ecg-*", "xray")
    assert ctx.can_use("ecg-1")
    assert not ctx.can_use("other")


async def test_all_failures_raise_identical_unauthorized(redis):
    good = await store_key(redis)
    _, _, unknown_id = new_key()
    wrong_secret = good[:-3] + ("AAA" if not good.endswith("AAA") else "BBB")
    expired = await store_key(redis, expires_at=int(time.time()) - 10)

    messages = set()
    for raw in [None, "", "garbage", unknown_id, wrong_secret, expired]:
        with pytest.raises(Unauthorized) as exc:
            await KeyStore(redis).authenticate(raw)
        messages.add(exc.value.message)
    assert len(messages) == 1


async def test_future_expiry_is_valid(redis):
    raw = await store_key(redis, expires_at=int(time.time()) + 3600)
    assert (await KeyStore(redis).authenticate(raw)).key_id


async def test_allow_all_flag(redis):
    raw = await store_key(redis, allowed_models=(), allow_all=True)
    assert (await KeyStore(redis).authenticate(raw)).can_use("anything")


async def test_corrupt_record_is_unauthorized_not_500(redis):
    key_id, _, raw = new_key()
    await redis.set(f"key:{key_id}", "{not json")
    with pytest.raises(Unauthorized):
        await KeyStore(redis).authenticate(raw)


async def test_record_with_wrong_types_is_unauthorized(redis):
    key_id, _, raw = new_key()
    await redis.set(f"key:{key_id}", json.dumps(["not", "a", "dict"]))
    with pytest.raises(Unauthorized):
        await KeyStore(redis).authenticate(raw)


class BrokenRedis:
    async def get(self, *_a, **_k):
        raise RedisConnectionError("down")


async def test_redis_failure_is_503():
    _, _, raw = new_key()
    with pytest.raises(RedisUnavailable):
        await KeyStore(BrokenRedis()).authenticate(raw)
