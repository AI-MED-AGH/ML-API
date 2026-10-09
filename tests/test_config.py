import pytest
from pydantic import ValidationError

from src.config import Settings


def test_defaults_match_spec():
    s = Settings(redis_url="redis://localhost:6379/0", _env_file=None)
    assert s.upstream_timeout == 60.0
    assert s.upstream_connect_timeout == 3.0
    assert s.max_body_bytes == 10 * 1024 * 1024
    assert s.activity_throttle_seconds == 10
    assert s.wake_pending_ttl_seconds == 60
    assert s.retry_after_seconds == 5


def test_reads_environment(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://:pw@redis:6379/1")
    monkeypatch.setenv("MAX_BODY_BYTES", "1024")
    s = Settings(_env_file=None)
    assert s.redis_url == "redis://:pw@redis:6379/1"
    assert s.max_body_bytes == 1024


def test_redis_url_is_required(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
