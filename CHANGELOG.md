# Changelog

## Unreleased

- Rate limits per API key (`RATE_LIMIT_PER_MINUTE`, `JOB_POLL_LIMIT_PER_MINUTE`, `JOB_SUBMIT_LIMIT_PER_DAY`, `MAX_OUTSTANDING_JOBS`),
  counted in Redis and checked before the request body is read. Admins can set them per key; models publish their own limits
  (per key, for all keys, concurrency) in their route. Refusals are `429` with `Retry-After`.
- A cap on unfinished jobs per key, and on model replies held in memory (`MAX_INFLIGHT_RESPONSE_BYTES`, `503 RouterBusy`).
  Replies over 1 MiB are shape-checked instead of fully parsed, and large job results are no longer re-encoded.
- Per-key request log and usage counters (`usage:<day>:<key>`), read by the Supervisor.
- Model 5xx bodies and failed jobs' error text are replaced by generic messages (`EXPOSE_UPSTREAM_ERRORS` to turn that off).
- `/docs` and `/openapi.json` are off unless `ENABLE_DOCS`; `/health` caches its Redis check; `GET /models` reads the
  Supervisor's route index instead of scanning Redis, and reads records in bulk.

## 0.2.0

- Routes are read from Redis (`route:<model>`), replacing the local `model_routes.json` file.
- API-key authentication with per-model scopes (`X-API-Key`); out-of-scope models return 404.
- **Breaking:** `POST /predict` now takes `{model, ...}` and forwards everything but `model` unchanged (`{data, metadata}`
  for default fastmlapi models, a typed model's own fields otherwise), replacing the `model_data` wrapper.
- Security: upstream redirects are never followed; invalid upstream statuses and `Retry-After` values are rejected;
  job ids can't be taken over (`SET NX`); upstream error text is not logged.
- New endpoints: `GET /models`, `GET /models/{name}/schema`, `GET /health`, `POST /jobs` and `GET /jobs/{job_id}` (proxy to queue-mode models; only the submitting key can poll a job).
- Uniform error responses, upstream timeouts, request and response size limits, wake requests for sleeping models.
- Removed the old `UvicornServer` wrapper (it returned raw exception text on 500).
