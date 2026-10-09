# Changelog

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
