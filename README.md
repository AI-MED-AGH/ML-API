# ML-API

A communication framework between client applications and machine learning models.

The Router is the public, authenticated entry point of the MLAPI platform. It checks a client's API key,
confirms the key may use the requested model, and forwards the request to the model container. It is
stateless: routes and keys live in Redis and are written by the MLAPI Supervisor.

Please read [How to use](doc/usage.md) for building and running a model container.

## Endpoints

All endpoints except `/health` need an `X-API-Key: mlapi_<id>_<secret>` header.

| Method & path | Purpose |
|---|---|
| `POST /predict` | Synchronous inference. Body `{"model": "name", ...}`: everything except `model` is forwarded unchanged to the model's `/predict`. Models that use `fastmlapi`'s default shape take `{"data": ..., "metadata": ...}`; models with a typed `request_model` take that model's own fields (`GET /models/{name}/schema` shows them) |
| `POST /jobs` | Submit work to a **queue-mode** model (long-running inference). Same body as `/predict`. Returns `202 {"job_id": "<model>~<id>"}` |
| `GET /jobs/{job_id}` | Status and result of a job you submitted (anyone else gets 404) |
| `GET /models` | Models this key may use: name, state, version |
| `GET /models/{name}/schema` | The model's cached request/response schema and example |
| `GET /health` | Unauthenticated: process and Redis |

A model the key may not use looks exactly like a model that does not exist (404).

### Errors

Errors from the Router use `{"success": false, "error": "...", "error_type": "...", "details": {}}`.
Responses coming from a model are passed through unchanged.

| Case | Status | `error_type` |
|---|---|---|
| Missing, malformed, unknown, expired key | 401 | `Unauthorized` |
| Unknown model or model outside the key's scope | 404 | `ModelNotFound` |
| `/predict` on a queue-mode model, or `/jobs` on a sync model | 400 | `WrongMode` |
| Invalid body | 422 | `ValidationError` |
| Body too large | 413 | `PayloadTooLarge` |
| Model not ready (starting, sleeping, ...) | 503 + `Retry-After`, `X-Model-Status` | `ModelUnavailable` |
| Model unreachable / invalid or oversized reply | 502 | `UpstreamError` |
| Model timeout | 504 | `UpstreamTimeout` |
| Redis unreachable | 503 | `AuthBackendUnavailable` |

## Configuration

Environment variables (see `.env.example`): `REDIS_URL` (required), `UPSTREAM_TIMEOUT`, `MAX_BODY_BYTES`,
`MAX_RESPONSE_BYTES`, `JOB_OWNER_TTL` (seconds a job stays pollable by its submitter, default 86400), `ACTIVITY_THROTTLE_SECONDS`, `LOG_LEVEL`.

## Running

```shell
uv sync
uv run uvicorn src.main:create_app --factory --reload
uv run pytest
```

Keys and routes are normally written by the Supervisor. For local development you can seed them by hand:

```shell
# route: states are ready | starting | sleeping | waiting_for_gpu | unavailable; mode is sync | queue
redis-cli SET route:my-model '{"url": "http://localhost:8001", "state": "ready", "mode": "sync"}'
# key: "hash" is sha256 of the secret part of mlapi_<12 chars>_<43 chars>
redis-cli SET key:<12-char-id> '{"hash": "<sha256 hex>", "allowed_models": ["my-model"], "allow_all": false, "expires_at": null, "name": "dev"}'
```
