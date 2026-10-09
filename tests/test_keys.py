import pytest

from src.auth.keys import (
    AuthContext,
    hash_secret,
    is_expired,
    model_allowed,
    parse_key,
    verify_secret,
)
from tests.helpers import new_key


def test_parse_valid_key():
    key_id, secret, raw = new_key()
    parsed = parse_key(raw)
    assert parsed is not None
    assert parsed.key_id == key_id
    assert parsed.secret == secret


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "mlapi_",
        "mlapi_short_x",
        "bearer abc",
        "mlapi_" + "a" * 12 + "_" + "b" * 42,   # secret one char too short
        "mlapi_" + "a" * 12 + "_" + "b" * 44,   # one char too long
        "mlapi_" + "a" * 11 + "!_" + "b" * 43,  # bad id char
        "mlapi_" + "a" * 12 + "_" + "b" * 42 + "!",
        "mlapi_" + "a" * 12 + "_" + "b" * 43 + "\n",   # trailing newline
        "mlapi_" + "ａ" * 12 + "_" + "b" * 43,         # full-width letters
    ],
)
def test_parse_rejects_malformed(raw):
    assert parse_key(raw) is None


def test_hash_and_verify():
    h = hash_secret("s3cret")
    assert len(h) == 64
    assert verify_secret("s3cret", h)
    assert not verify_secret("other", h)


def test_verify_rejects_garbage_hash():
    assert not verify_secret("x", "")
    assert not verify_secret("x", "not-hex")


@pytest.mark.parametrize(
    "model,allowed,allow_all,expected",
    [
        ("ecg-a", ["ecg-a"], False, True),
        ("ecg-b", ["ecg-a"], False, False),
        ("ecg-b", ["ecg-*"], False, True),
        ("ecg", ["ecg-*"], False, False),
        ("xecg-b", ["ecg-*"], False, False),
        ("anything", [], True, True),
        ("anything", [], False, False),
        ("anything", ["*"], False, False),
        ("a", ["a*"], False, True),
    ],
)
def test_model_allowed(model, allowed, allow_all, expected):
    assert model_allowed(model, allowed, allow_all) is expected


@pytest.mark.parametrize(
    "expires_at,now,expected",
    [(None, 100, False), (200, 100, False), (100, 100, True), (50, 100, True)],
)
def test_is_expired(expires_at, now, expected):
    assert is_expired(expires_at, now) is expected


def test_auth_context_can_use():
    ctx = AuthContext(key_id="k", allowed_models=("ecg-*",), allow_all=False)
    assert ctx.can_use("ecg-1")
    assert not ctx.can_use("xray")
    assert AuthContext("k", (), True).can_use("xray")


def test_verify_rejects_non_ascii_hash_without_raising():
    assert not verify_secret("x", "héllo")
