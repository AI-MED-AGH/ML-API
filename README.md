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
| Over a rate limit | 429 | `RateLimited` (with `Retry-After`) |
| Router holding too many model replies in memory | 503 | `RouterBusy` (with `Retry-After`) |

## Configuration

Environment variables (see `.env.example`): `REDIS_URL` (required), `UPSTREAM_TIMEOUT`, `MAX_BODY_BYTES`,
`MAX_RESPONSE_BYTES`, `JOB_OWNER_TTL` (seconds a job stays pollable by its submitter, default 86400), `ACTIVITY_THROTTLE_SECONDS`, `LOG_LEVEL`.

Limits (all per API key, shared by every Router replica through Redis; `0` turns a limit off):
`RATE_LIMIT_PER_MINUTE` (predict calls and job submissions together, default 120), `JOB_POLL_LIMIT_PER_MINUTE` (default 600),
`JOB_SUBMIT_LIMIT_PER_DAY` (default 1000; a fixed window, so a client can use two days' worth around midnight UTC).
Requests are counted after the key is verified and before the body is read; wrong keys never use up anyone's budget.

Per key, an admin can override each of these on the key itself (`limits` on `POST`/`PATCH /v1/keys` in the Supervisor).
`MAX_OUTSTANDING_JOBS` (default 100) is how many unfinished jobs a key may have at once: a job counts until a poll shows it
succeeded or failed. A model can also publish its own limits (per key, for all keys, and concurrent requests); those and the
defaults for models that publish none (`MODEL_RPM_PER_KEY`, `MODEL_RPM_TOTAL`, `MODEL_MAX_CONCURRENCY` = 16) are described in
the platform contracts. Concurrency is per Router process and refuses with 429 `ModelBusy` instead of queueing.

Every request with a valid key is logged (`key=… model=… status=… ms=…`, never the secret) and counted per key, model and UTC
day (`USAGE_TTL_DAYS`, default 35; the Supervisor shows it at `GET /v1/keys/{id}/usage`).

`EXPOSE_UPSTREAM_ERRORS` (default off): a model's 5xx reply body, and the error text of a failed job, are replaced by a generic
message, because they can contain file paths and internals. 4xx bodies (the model explaining a bad input) still pass through.
`ENABLE_DOCS` (default off) serves `/docs` and `/openapi.json`. `HEALTH_CACHE_SECONDS` (default 2) is how long `/health` reuses
its last Redis check.

`MAX_INFLIGHT_RESPONSE_BYTES` (default 128 MiB) caps the model replies one Router process holds in memory at once,
from when they are read from the model until they have been sent to the client; peak use is
about twice that, so keep it well under the pod's memory limit. Replies above 1 MiB are only checked for a JSON object/array
shape, not fully parsed, because parsing blocks the whole replica; a failed job's reply that large is replaced by a generic one.

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

## Trying a model in the browser

`ui-tester/` is a small dev UI (Vite + React) for testing inference against a running Router. It is a dev/showcase tool,
not production software, and it is not part of the compose stack.

```shell
cd ui-tester
npm install
ROUTER_URL=http://127.0.0.1:8000 npm run dev   # http://localhost:5173
```

Enter an API key (create one in the Supervisor admin UI). The UI lists the models the key can use, loads each model's
schema and example payload, and sends `/predict` (sync models) or `/jobs` and polls `/jobs/{id}` (queue models). For each
call it shows the status, `X-Model-Status`, `Retry-After`, latency and the response body. The dev server proxies `/api/*`
to the Router, so no CORS setup is needed, and the key is kept in `sessionStorage` only. See [ui-tester/README.md](ui-tester/README.md).
