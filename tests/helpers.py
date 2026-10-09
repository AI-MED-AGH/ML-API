import base64
import hashlib
import json
import secrets
import string


def new_key(key_id: str | None = None) -> tuple[str, str, str]:
    key_id = key_id or "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(12)
    )
    secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    return key_id, secret, f"mlapi_{key_id}_{secret}"


async def store_key(
    redis,
    *,
    allowed_models=("m1",),
    allow_all=False,
    expires_at=None,
    name="test",
) -> str:
    key_id, secret, raw = new_key()
    await redis.set(
        f"key:{key_id}",
        json.dumps(
            {
                "hash": hashlib.sha256(secret.encode()).hexdigest(),
                "allowed_models": list(allowed_models),
                "allow_all": allow_all,
                "expires_at": expires_at,
                "name": name,
            }
        ),
    )
    return raw


async def store_route(
    redis,
    model: str,
    *,
    state: str = "ready",
    url: str = "http://m-x.mlapi-models.svc:8000",
    mode: str = "sync",
) -> None:
    await redis.set(
        f"route:{model}",
        json.dumps({"url": url, "state": state, "mode": mode}),
    )
