import json
import logging
import time

from redis.exceptions import RedisError

from src.auth.keys import AuthContext, is_expired, parse_key, verify_secret
from src.common.errors import RedisUnavailable, Unauthorized

logger = logging.getLogger(__name__)

_DUMMY_HASH = "0" * 64
_UNAUTHORIZED = "Invalid or missing API key"


class KeyStore:
    def __init__(self, redis):
        self._redis = redis

    async def authenticate(
        self, raw_key: str | None, now: float | None = None
    ) -> AuthContext:
        parsed = parse_key(raw_key)
        if parsed is None:
            raise Unauthorized(_UNAUTHORIZED)

        try:
            raw_record = await self._redis.get(f"key:{parsed.key_id}")
        except RedisError:
            logger.exception("Redis error while reading API key")
            raise RedisUnavailable("Authentication backend unavailable")

        record = self._load(raw_record)
        # Always run one hash comparison so unknown ids and wrong secrets cost the same.
        stored_hash = record["hash"] if record else _DUMMY_HASH
        secret_ok = verify_secret(parsed.secret, stored_hash)
        if record is None or not secret_ok:
            raise Unauthorized(_UNAUTHORIZED)
        if is_expired(record.get("expires_at"), now if now is not None else time.time()):
            raise Unauthorized(_UNAUTHORIZED)

        return AuthContext(
            key_id=parsed.key_id,
            allowed_models=tuple(record.get("allowed_models") or ()),
            allow_all=record.get("allow_all") is True,
        )

    @staticmethod
    def _load(raw_record: str | None) -> dict | None:
        if raw_record is None:
            return None
        try:
            record = json.loads(raw_record)
        except ValueError:
            logger.warning("Corrupt API key record")
            return None
        if not isinstance(record, dict) or not isinstance(record.get("hash"), str):
            logger.warning("Malformed API key record")
            return None
        return record
