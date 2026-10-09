import json

from tests.helpers import store_key, store_route


async def seed(redis):
    await store_route(redis, "ecg-1", state="ready")
    await store_route(redis, "ecg-2", state="sleeping")
    await store_route(redis, "xray", state="ready")
    await redis.set(
        "schema:ecg-1",
        json.dumps({"request_schema": {"type": "object"}, "response_schema": {},
                    "example": {"data": 1}, "model_version": "1.2.0"}),
    )
    return {"X-API-Key": await store_key(redis, allowed_models=("ecg-*",))}


async def test_catalog_lists_only_models_in_scope(client, redis):
    headers = await seed(redis)
    r = await client.get("/models", headers=headers)
    assert r.status_code == 200
    assert r.json() == {
        "models": [
            {"name": "ecg-1", "state": "ready", "version": "1.2.0"},
            {"name": "ecg-2", "state": "sleeping", "version": None},
        ]
    }
    assert "xray" not in r.text


async def test_catalog_requires_auth(client):
    assert (await client.get("/models")).status_code == 401


async def test_schema_endpoint_returns_cached_schema(client, redis):
    headers = await seed(redis)
    r = await client.get("/models/ecg-1/schema", headers=headers)
    assert r.status_code == 200
    assert r.json()["request_schema"] == {"type": "object"}
    assert r.json()["model_version"] == "1.2.0"


async def test_schema_hides_out_of_scope_unknown_and_unschemaed_identically(client, redis):
    headers = await seed(redis)
    texts = set()
    for name in ("xray", "nope", "ecg-2"):  # out of scope, unknown, no schema cached
        r = await client.get(f"/models/{name}/schema", headers=headers)
        assert r.status_code == 404
        texts.add(r.text)
    assert len(texts) == 1


async def test_schema_rejects_hostile_names(client, redis):
    headers = await seed(redis)
    r = await client.get("/models/..%2Fetc/schema", headers=headers)
    assert r.status_code == 404
